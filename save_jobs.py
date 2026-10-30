"""App-only cancellable saves. Secret-bearing input stays in process memory.

A process restart never resumes a save. Candidate engine providers are fresh,
unbound generations; interrupted jobs can require cleanup but cannot route.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import threading
import time
import uuid

from storage import MODEL_ADAPTERS, ProfileStore, validate_models, validate_model_settings, merge_discovered_model_settings
from engine_control import EngineManager, activate_provider, activation_lock, keyring, public_aliases
import probe

READY_TTL_SECONDS=300

TERMINAL={"succeeded","failed","cancelled","interrupted","committed_cleanup_required"}


class JobCancelled(ValueError):
    pass


class SaveJobs:
    def __init__(self, store_factory=ProfileStore, candidate_fn=None, manager_factory=EngineManager, keyring_fn=None, publish_fn=None):
        self.store_factory=store_factory
        self.candidate_fn=candidate_fn
        self.manager_factory=manager_factory
        self.keyring_fn=keyring_fn
        self.publish_fn=publish_fn
        self.jobs={}
        self.lock=threading.Lock()
        self.owner=uuid.uuid4().hex

    def _tables(self,store):
        with store.connection() as db:
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('provider_save_jobs','staged_engine_candidates')").fetchone()[0]==2:
                return
        with store.atomic() as db:
            db.execute("CREATE TABLE IF NOT EXISTS provider_save_jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS staged_engine_candidates(job_id TEXT NOT NULL, name TEXT PRIMARY KEY, account TEXT NOT NULL)")

    def _record(self,job,db=None):
        document={k:deepcopy(v) for k,v in job.items() if k in {"id","state","total","tested","current_model","short_tests_started","capability_lookups_started","request_attempts","inference_requests","metadata_requests","cleanup_pending","result","message","ready_expires_at"}}
        values=(job["id"],self.owner,os.getpid(),job["state"],json.dumps(document,ensure_ascii=True))
        if db is not None:
            db.execute("INSERT OR REPLACE INTO provider_save_jobs VALUES (?,?,?,?,?)",values)
        else:
            with self.store_factory().atomic() as connection:
                connection.execute("INSERT OR REPLACE INTO provider_save_jobs VALUES (?,?,?,?,?)",values)

    def start(self, *, client_job_id, **args):
        # The client owns this UUID before sending anything. First registration
        # wins, even across managers; retries only query it and never restart it.
        try: ident=str(uuid.UUID(client_job_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("client_job_id 须为客户端预先生成的 UUID。") from None
        with self.lock: known=ident in self.jobs
        if known: return self.status(ident)
        store=self.store_factory(); self._tables(store)
        with store.connection() as db:
            exists=db.execute("SELECT 1 FROM provider_save_jobs WHERE id=?",(ident,)).fetchone()
        if exists: return self.status(ident)
        with self.lock:
            job={"id":ident,"state":"queued","total":len(args.get("models") or [args.get("model")]),
                 "tested":0,"current_model":None,"short_tests_started":0,"capability_lookups_started":0,
                 "request_attempts":0,"inference_requests":0,"metadata_requests":0,
                 "cancel":threading.Event(),"commit":threading.Event(),"lock":threading.RLock()}
            with store.atomic() as db:
                exists=db.execute("SELECT 1 FROM provider_save_jobs WHERE id=?",(ident,)).fetchone()
                if not exists: self._record(job,db)
            if not exists:
                self.jobs[ident]=job
                thread=threading.Thread(target=self._run,args=(job,{"args":deepcopy(args)}),daemon=True)
                job["thread"]=thread
                try: thread.start()
                except Exception:
                    job["state"]="failed";job["message"]="后台线程未启动，未保存。"
                    self._record(job);job.pop("thread",None)
        return self.status(ident)

    def commit(self,ident):
        with self.lock: job=self.jobs.get(ident)
        if not job: return {"ok":False,"code":"job_not_owned","job_id":ident,"status":self.status(ident)}
        if not job["lock"].acquire(blocking=False):
            return {"ok":False,"job_id":ident,"code":"job_busy","message":"状态正在更新，请查询任务状态。"}
        try:
            if job["commit"].is_set() or job["state"] in TERMINAL:
                return self.status(ident)
            if job["state"]!="ready":
                return {"ok":False,"code":"job_not_ready","job_id":ident,"state":job["state"]}
            if time.monotonic() >= job["ready_deadline"]:
                self._record(dict(job,state="cancel_requested",message="等待提交已过期，未保存连接。"))
                job["cancel_reason"]="等待提交已过期，未保存连接。"
                job["cancel"].set();job["state"]="cancel_requested";job["commit"].set()
                return {"ok":False,"code":"job_expired","job_id":ident,"state":"cancel_requested"}
            # Persist authorization before waking the worker. A lost reply is
            # recoverable with the UUID; restart never replays this authorization.
            self._record(dict(job,state="commit_requested"))
            job["state"]="commit_requested";job["commit"].set()
            return self.status(ident)
        finally: job["lock"].release()

    def status(self,ident):
        with self.lock: local=self.jobs.get(ident)
        if local and local["lock"].acquire(blocking=False):
            try:
                result={k:deepcopy(v) for k,v in local.copy().items() if k in {"id","state","total","tested","current_model","short_tests_started","capability_lookups_started","request_attempts","inference_requests","metadata_requests","cleanup_pending","result","message","ready_expires_at"}}
                return dict(result,ok=True,job_id=ident)
            finally: local["lock"].release()
        store=self.store_factory()
        # Do not take a write lock while a worker is committing.
        with store.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_save_jobs'").fetchone():
                return {"ok":False,"code":"job_not_found"}
            row=db.execute("SELECT owner,pid,state,metadata FROM provider_save_jobs WHERE id=?",(ident,)).fetchone()
        if not row: return {"ok":False,"code":"job_not_found"}
        result=json.loads(row[3]); result["ok"]=True;result["job_id"]=ident
        result["recorded_state"]=row[2]
        if row[0]!=self.owner and row[2] not in TERMINAL:
            if row[2] in {"committed","finalizing"}:
                result["state"]="committed_cleanup_required"
                result["cleanup_pending"]=True
                result["message"]="连接已提交，原管理器最终化未确认；请核查清理，不能当作未保存重试。"
            else:
                result["state"]="detached"
                with store.connection() as db:
                    result["cleanup_pending"]=bool(db.execute("SELECT 1 FROM staged_engine_candidates WHERE job_id=? LIMIT 1",(ident,)).fetchone())
                result["message"]="原管理器任务不可在本进程续存；确认原进程退出后仅可清理未发布候选。"
        return result

    def cancel(self,ident):
        with self.lock: job=self.jobs.get(ident)
        if not job: return {"ok":False,"code":"job_not_owned","message":"任务属于另一管理器或已中断，不能声称已取消。"}
        if not job["lock"].acquire(blocking=False):
            committing=job["state"] in {"committing","finalizing"} or job.get("committed")
            return {"ok":False,"code":"commit_in_progress" if committing else "job_busy",
                    "message":"已进入原子提交阶段，请查询最终状态。" if committing else "状态正在更新，未确认取消，请重试或查询状态。"}
        try:
            if job["state"] in TERMINAL or job.get("committed"):
                return {"ok":False,"code":"already_finished","state":job["state"]}
            self._record(dict(job,state="cancel_requested"))
            job["cancel"].set();job["state"]="cancel_requested";job["commit"].set()
            return {"ok":True,"job_id":ident,"state":"cancel_requested","message":"已禁止后续提交，正在清理未发布候选。"}
        finally: job["lock"].release()

    def _run(self,job,payload):
        store=self.store_factory(); args=payload["args"]
        def check():
            if job["cancel"].is_set(): raise JobCancelled(job.get("cancel_reason","任务已取消，未提交连接。"))
        def progress(**changes):
            with job["lock"]:
                check();job.update(changes);self._record(job)
        @contextmanager
        def guard():
            with job["lock"]:
                check();self._record(dict(job,state="committing"));job["state"]="committing"
                yield
                job["committed"]=True
                job["state"]="finalizing"
        def save_candidate():
            check()
            return store.save(args["name"],args["protocol"],args.get("base_url",""),selected[0],args.get("key",""),
                              original or None,new_id=None if original else ident,models=selected,per_model_settings=settings,
                              expected_existing=(original,payload["fingerprint"]) if original else None)
        def committed(db,revision=None):
            if revision is None:
                row=db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?",(ident,)).fetchone()
                revision=store._content_revision(db,row)
            aliases={row[1]:row[0] for row in db.execute("SELECT alias,model FROM engine_bindings WHERE provider_id=?",(ident,))}
            receipts=[]
            for model,effort in tested_efforts.items():
                alias=aliases.get(model)
                db.execute("INSERT INTO model_probe_receipts(provider_id,model,revision,alias,effort,job_id) VALUES (?,?,?,?,?,?)",(ident,model,revision,alias,effort or "",job["id"]))
                receipts.append({"model":model,"effort":effort,"alias":alias,"revision":revision})
            job["result"]={"ok":True,"profile":{"id":ident,"model":selected[0],"models":selected},
                           "activation":{"ok":True,"kind":"jev" if args["protocol"]=="system_one" else "subscription" if args["protocol"]=="codex_subscription" else "models", "catalog_updated":args["protocol"]!="system_one"},
                           "detected_models":detected,"probe_receipts":receipts,"tested_models":list(tested_efforts),
                           "detected":dict(detected[selected[0]],source=("未查询" if selected[0] not in tested_efforts else
                                           "本机现有模型目录" if args["protocol"]=="codex_subscription" else "服务商模型目录"))}
            if credentials_changed and any(model in previous_models and model not in tested_efforts for model in selected):
                job["result"]["warnings"]=["地址或密钥已变更，已有模型未重新测试。"]
            if args["protocol"] in MODEL_ADAPTERS:
                job["result"]["activation"]["aliases"]=[row[0] for row in db.execute("SELECT alias FROM engine_bindings WHERE provider_id=? ORDER BY alias",(ident,))]
            db.execute("DELETE FROM staged_engine_candidates WHERE job_id=?",(job["id"],))
            self._record(dict(job,state="committed"),db)
        def journal(name,account):
            check()
            with store.atomic() as db:
                db.execute("INSERT INTO staged_engine_candidates VALUES (?,?,?)",(job["id"],name,account))
                db.execute("INSERT OR IGNORE INTO pending_credential_cleanup VALUES (?)",(account,))
        def observe(kind):
            with job["lock"]:
                check()
                job["request_attempts"]+=1
                job["inference_requests" if kind=="inference" else "metadata_requests"]+=1
                self._record(job)
        observer_token=probe.REQUEST_OBSERVER.set(observe)
        try:
            from mcp_server import _candidate
            selected=validate_models(args["model"],args.get("models"),args["protocol"])
            overrides=args.get("per_model_overrides") or {}
            fields={"context_window","compact_limit","reasoning_levels","default_effort"}
            selected_set=set(selected)
            if not isinstance(overrides,dict) or any(m not in selected_set or not isinstance(v,dict) or set(v)-fields for m,v in overrides.items()):
                raise ValueError("逐模型设置包含未选模型或无效字段。")
            original=args.get("provider_id") or ""
            route,secret,fingerprint=(self.candidate_fn or _candidate)(store,args["name"],args["protocol"],args.get("base_url",""),args["model"],args.get("key",""),original)
            ident=original or str(uuid.uuid4())
            previous_models=set(store.model_ids(original)) if original else set()
            prior_profile=next((p for p in store.list() if p["id"]==original),None) if original else None
            credentials_changed=bool(prior_profile and args["protocol"]!="codex_subscription" and
                (route["protocol"]!=prior_profile["protocol"] or route["base_url"]!=prior_profile["base_url"] or args.get("key","").strip()))
            to_test=[model for model in selected if args.get("retest_existing") or model not in previous_models]
            test_set=set(to_test)
            progress(total=len(to_test))
            if args["protocol"] in MODEL_ADAPTERS: public_aliases(store,ident,selected)
            configured={}
            previous_settings=store.all_model_settings(original) if original else {}
            for model in selected:
                base=({field:args.get(field) for field in fields} if model==selected[0] else dict(previous_settings.get(model,dict.fromkeys(fields))))
                base.update(overrides.get(model,{}))
                configured[model]=validate_model_settings(model,**base)
            payload.update(fingerprint=fingerprint)
            check()
            settings={};detected={};tested_efforts={}
            for model in selected:
                check()
                if model not in test_set:
                    settings[model]=configured[model]
                    detected[model]={}
                    continue
                progress(state="testing",current_model=model,short_tests_started=job["short_tests_started"]+1)
                candidate=dict(route,model=model)
                def short_test(c=candidate):
                    if args["protocol"]=="system_one": return probe.probe_jev(c,secret)
                    if args["protocol"]=="codex_subscription": return probe.probe_official(c["model"],store.root)
                    return probe.probe_snapshot(c,secret,None)
                tested=probe.bounded_read(short_test,time.monotonic()+probe.PROBE_JOB_TIMEOUT_SECONDS,check)
                if not tested.get("ok"):
                    job["result"]={"ok":False,"code":"probe_failed","failed_model":model,"detail":tested.get("code","probe_failed"),
                                   "message":model+"："+probe.describe_probe_failure(tested)+"未保存或修改连接。"}
                    for field in ("http_status", "upstream_error_type", "request_id"):
                        if field in tested: job["result"][field]=tested[field]
                    progress(state="failed",tested=job["tested"]+1);return
                tested_efforts[model]=None
                found={}
                if args["protocol"] in MODEL_ADAPTERS:
                    progress(capability_lookups_started=job["capability_lookups_started"]+1)
                    found=probe.bounded_read(lambda c=candidate:probe.discover_capabilities(c,secret),time.monotonic()+12,check)
                elif args["protocol"]=="codex_subscription": found=probe.official_capabilities(store.root,model)
                value=dict(configured[model])
                settings[model]=merge_discovered_model_settings(model,value,found);detected[model]=found
                progress(tested=job["tested"]+1)
            with job["lock"]:
                job["ready_deadline"]=time.monotonic()+READY_TTL_SECONDS
                progress(state="ready",current_model=None,ready_expires_at=time.time()+READY_TTL_SECONDS)
            if not job["commit"].wait(max(0,job["ready_deadline"]-time.monotonic())):
                with job["lock"]:
                    if not job["commit"].is_set():
                        job["cancel"].set()
                        job["cancel_reason"]="等待提交已过期，未保存连接。"
            check()
            progress(state="staging")
            with activation_lock(store.root):
                check()
                if original and store.edit_fingerprint(original)!=payload["fingerprint"]:
                    raise ValueError("连接在测试期间发生变化，请重新操作。")
                if args["protocol"] in MODEL_ADAPTERS:
                    manager=self.manager_factory(store.root)
                    from catalog import refresh
                    publish=(lambda:self.publish_fn(store,manager)) if self.publish_fn else lambda:refresh(store.root,store=store)
                    profile={"id":ident,"name":args["name"],"protocol":args["protocol"],"base_url":route["base_url"],
                             "model":selected[0],"models":selected,"_revision":"candidate"}
                    result=activate_provider(store,ident,manager,self.keyring_fn or (lambda a,b,c:keyring(a,b,c,store.root)),
                                             prepared=(profile,secret,settings),commit_candidate=save_candidate,
                                             commit_guard=guard,cancel_check=check,journal=journal,on_commit=committed,publish=publish)
                    if not result.get("ok"):
                        if job["cancel"].is_set():
                            job["cleanup_pending"]=not result.get("rollback_ok",True)
                            raise JobCancelled("任务已取消，未提交连接。"+("候选清理未完成，请使用重试凭据清理入口。" if job["cleanup_pending"] else ""))
                        job["cleanup_pending"]=not result.get("rollback_ok",True)
                        raise ValueError("整组启用失败（"+result.get("code","unknown")+"），未提交连接。"+
                                         ("候选清理未完全确认。" if not result.get("rollback_ok",True) else ""))
                    # Do not claim an untested existing model was just tested.
                    if set(tested_efforts)==set(selected):
                        from mcp_server import _remember_pretest
                        profile,_=store.resolve_profile(ident);_remember_pretest(ident,profile["_revision"])
                    try:
                        from engine_control import cleanup_unpublished_providers
                        pending=cleanup_unpublished_providers(store,manager,self.keyring_fn or (lambda a,b,c:keyring(a,b,c,store.root)))
                        job["result"]["credential_cleanup_pending"]=bool(pending)
                        job["cleanup_pending"]=bool(pending)
                    except Exception:
                        job["result"]["credential_cleanup_pending"]=True
                        job["cleanup_pending"]=True
                else:
                    with guard(),store.atomic() as db:
                        save_candidate()
                        if args["protocol"]=="system_one": store.set_jev_profile(ident)
                        if args["protocol"]=="codex_subscription":
                            store.enable_connection(ident)
                            from catalog import refresh
                            from catalog import transactional_publish
                            transactional_publish(store,lambda:refresh(store.root,store=store))
                        check();committed(db)
            # The activation lock is released before exposing a terminal success.
            with job["lock"]:
                job["state"]="committed_cleanup_required" if job.get("cleanup_pending") else "succeeded"
                self._record(job)
            # Persist only metadata; credentials are released with this worker.
        except JobCancelled as error:
            job["state"]="cancelled";job["message"]=str(error);self._record(job)
        except Exception as error:
            if job.get("committed"):
                job["state"]="committed_cleanup_required"
                job["cleanup_pending"]=True
                job["message"]="连接已原子提交；后续回执或清理需要检查。"
                self._record(job);return
            job.pop("result",None)
            if hasattr(error,"model"):
                job["result"]={"ok":False,"code":"model_alias_invalid","failed_model":error.model}
            job["state"]="cancelled" if job["cancel"].is_set() else "failed"
            job["message"]=str(error) if isinstance(error,ValueError) else "后台保存失败，未报告成功；请检查候选清理状态。"
            self._record(job)
        finally:
            probe.REQUEST_OBSERVER.reset(observer_token)
            payload.clear()
            job.pop("thread",None)
