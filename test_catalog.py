import os
import tempfile
import unittest
from unittest.mock import patch
from test_neutral_python_compat import isolated_native_catalog

from catalog import compile_catalog
from storage import ProfileStore


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="catalog-native-")
        self.addCleanup(temp.cleanup)
        isolated_native_catalog(self, temp.name)

    def test_hover_description_uses_connection_name_for_existing_and_new_aliases(self):
        with tempfile.TemporaryDirectory(prefix="hub-readable-route-") as root:
            store = ProfileStore(root)
            aliases = []
            for name in ("connection-a", "另一条线路"):
                profile = store.save(name, "responses", "https://example.invalid/v1", "same-model", "fake-key")
                snapshot, _ = store.resolve_profile(profile["id"])
                engine_name = "hub-" + profile["id"].replace("-", "")
                alias = engine_name + "/same-model"
                store.bind_engine_route(alias, profile["id"], snapshot["_revision"],
                                        engine_name + "/7b1c22c4-1151-418a-a137-6341ff54fcaf")
                aliases.append(alias)
            baseline = {"models": [{"slug": "native-alpha", "description": "Official"},
                                   {"slug": aliases[0], "description": "Routed via hub-internal"}]}
            engine = {"models": [{"slug": alias, "description": "Routed via hub-internal"} for alias in aliases]}
            rows = {row["slug"]: row for row in compile_catalog(baseline, engine, store)["models"]}
            self.assertEqual(rows["native-alpha"]["description"], "Official")
            self.assertEqual(rows[aliases[0]]["description"], "供应商：connection-a")
            self.assertEqual(rows[aliases[1]]["description"], "供应商：另一条线路")
            for alias in aliases:
                self.assertEqual(rows[alias]["display_name"], "same-model")
                self.assertNotIn("hub-", rows[alias]["description"])

    def test_settings_override_existing_bound_model_without_losing_other_metadata(self):
        with tempfile.TemporaryDirectory(prefix="hub-catalog-settings-") as root:
            store = ProfileStore(root)
            settings = {"context_window": 64000, "compact_limit": 50000,
                        "reasoning_levels": ["high", "max"], "default_effort": "high"}
            p = store.save("External", "chat_completions", "https://example.invalid/v1", "fixture-beta", "key", settings=settings)
            profile, _ = store.resolve_profile(p["id"])
            alias = "fixture-route/fixture-beta"
            store.bind_engine_route(alias, p["id"], profile["_revision"], "fixture-route/7b1c22c4-1151-418a-a137-6341ff54fcaf")
            baseline = {"models": [{"slug": alias, "display_name":"Old fixture display", "context_window": 1000000, "comp_hash": "keep",
                                    "supported_reasoning_levels": [{"effort": "max", "description": "Maximum"}]}]}
            result = compile_catalog(baseline, {"models": [{"slug": alias}]}, store)["models"][0]
            self.assertEqual((result["context_window"], result["auto_compact_token_limit"]), (64000, 50000))
            self.assertEqual(result["default_reasoning_level"], "high")
            self.assertEqual([x["effort"] for x in result["supported_reasoning_levels"]], ["high", "max"])
            self.assertEqual(result["comp_hash"], "keep")
            self.assertEqual(result["display_name"], "fixture-beta")

    def test_official_subscription_settings_override_matching_catalog_model(self):
        with tempfile.TemporaryDirectory(prefix="hub-official-settings-") as root:
            store = ProfileStore(root)
            store.save("Official", "codex_subscription", "", "native-alpha", settings={
                "context_window": 256000, "compact_limit": 230000,
                "reasoning_levels": ["medium", "high"], "default_effort": "medium"})
            row = compile_catalog({"models": [{"slug": "native-alpha", "context_window": 1000000}]},
                                  {"models": []}, store)["models"][0]
            self.assertEqual((row["context_window"], row["auto_compact_token_limit"]), (256000, 230000))
            self.assertEqual(row["default_reasoning_level"], "medium")

    def test_disabled_bound_model_is_hidden_even_if_baseline_contains_it(self):
        from engine_control import disable_provider
        with tempfile.TemporaryDirectory(prefix="hub-disabled-catalog-") as root:
            store = ProfileStore(root)
            p = store.save("External", "chat_completions", "https://example.invalid/v1", "fixture-beta", "key")
            alias = "fixture-route/fixture-beta"
            profile, _ = store.resolve_profile(p['id'])
            store.bind_engine_route(alias, p['id'], profile['_revision'], 'fixture-route/7b1c22c4-1151-418a-a137-6341ff54fcaf')
            account = store.get_engine_binding(alias)['account']
            class Manager:
                current = {'apiKey':'keychain:'+account,'adapter':'openai-chat','baseUrl':'https://example.invalid/v1','models':['fixture-beta'],'defaultModel':'fixture-beta'}
                def snapshot(self): return {'providers':{'fixture-route':self.current}}
                def replace_provider(self, name, value): self.current = value; return True
            self.assertTrue(disable_provider(store, p['id'], Manager())['ok'])
            base = {'models':[{'slug':alias, 'context_window':1000000, 'visibility':'list'}]}
            row = compile_catalog(base, {'models':[]}, store)['models'][0]
            self.assertEqual(row['visibility'], 'hide')

    def test_baseline_is_preserved_and_new_bound_models_are_added(self):
        with tempfile.TemporaryDirectory(prefix="hub-catalog-test-") as root:
            store = ProfileStore(root)
            profile = store.save("External", "responses", "https://example.invalid/v1", "model-a", "fake-key")
            snapshot, _ = store.resolve_profile(profile["id"])
            name = "hub-" + profile["id"].replace("-", "")
            alias = name + "/model-a"
            account = name + "/7b1c22c4-1151-418a-a137-6341ff54fcaf"
            store.bind_engine_route(alias, profile["id"], snapshot["_revision"], account)
            baseline = {"models": [{"slug":"native-alpha", "context_window":1000000, "comp_hash":"3000"},
                                    {"slug":"fixture-beta", "visibility":"list"}]}
            engine = {"models": [{"slug":alias,"display_name":"old", "context_window":32768, "comp_hash":"engine"}]}
            result = compile_catalog(baseline, engine, store)
            self.assertEqual(result["models"][0], baseline["models"][0])
            self.assertEqual(result["models"][1]["visibility"], "hide")
            self.assertEqual(result["models"][2]["display_name"], "model-a")
            self.assertIsNone(result["models"][2]["comp_hash"])
            self.assertEqual(result["models"][2]["context_window"], 32768)
            self.assertNotIn("auto_compact_token_limit", result["models"][2])
            self.assertNotIn("supported_reasoning_levels", result["models"][2])
            self.assertNotIn("default_reasoning_level", result["models"][2])
            with self.assertRaises(ValueError):
                compile_catalog(baseline, {"models":[]}, store)

    def test_multiple_models_in_one_connection_have_model_names_and_keep_routes(self):
        with tempfile.TemporaryDirectory(prefix="hub-catalog-names-") as root:
            store=ProfileStore(root)
            profile=store.save('Shared route','responses','https://example.invalid/v1','model-a','fake-key',models=['model-a','model-b'])
            snapshot,_=store.resolve_profile(profile['id'])
            name='hub-'+profile['id'].replace('-','')
            aliases=[name+'/model-a',name+'/model-b']
            for i,alias in enumerate(aliases):
                account=name+f'/7b1c22c4-1151-418a-a137-6341ff54fca{i}'
                store.bind_engine_route(alias,profile['id'],snapshot['_revision'],account,model=alias.split('/')[-1])
            baseline={'models':[{'slug':'native-alpha','display_name':'Official Terra'}]}
            engine={'models':[{'slug':alias,'display_name':'Shared route'} for alias in aliases]}
            result=compile_catalog(baseline,engine,store)['models']
            self.assertEqual(result[0]['display_name'],'Official Terra')
            self.assertEqual([(x['slug'],x['display_name']) for x in result[1:]],list(zip(aliases,['model-a','model-b'])))

    def test_slash_in_upstream_model_uses_the_engine_advertised_alias(self):
        with tempfile.TemporaryDirectory(prefix="hub-catalog-slash-") as root:
            store=ProfileStore(root)
            profile=store.save("Nested", "chat_completions", "https://example.invalid/v1", "vendor/model", "fake-key")
            snapshot,_=store.resolve_profile(profile['id'])
            name="hub-"+profile['id'].replace("-", "")
            alias=name+"/vendor/model"
            store.bind_engine_route(alias,profile['id'],snapshot['_revision'],name+"/7b1c22c4-1151-418a-a137-6341ff54fcaf")
            result=compile_catalog({"models":[]},{"models":[{"slug":name+"/vendor-model","context_window":32768}]},store)
            self.assertEqual(result['models'][0]['slug'],alias)


if __name__ == "__main__": unittest.main()
