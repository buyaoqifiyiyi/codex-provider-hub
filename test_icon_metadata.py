"""Verify the sidebar entry receives the same packaged light/dark icons."""
import base64
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parent


class IconMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_and_global_tool_publish_packaged_icons(self):
        with tempfile.TemporaryDirectory(prefix='hub-icon-mcp-') as tmp:
            env=dict(os.environ,LOCALAPPDATA=tmp,CODEX_HOME=str(Path(tmp)/'codex'),
                     HTTPS_PROXY='http://127.0.0.1:9',https_proxy='http://127.0.0.1:9')
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp_server.py')],env=env,cwd=str(ROOT))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    initialized=await session.initialize()
                    tools={t.name:t for t in (await session.list_tools()).tools}
                    manager=tools['open_provider_manager']
                    self.assertEqual({x['type'] for x in manager.meta['openai/ui']['entrypoints']},{'global','thread'})
                    for icons in (initialized.server_info.icons,manager.icons):
                        self.assertEqual({icon.theme for icon in icons},{'light','dark'})
                        for icon in icons:
                            self.assertEqual(icon.mime_type,'image/png')
                            self.assertEqual(icon.sizes,['128x128'])
                            self.assertTrue(icon.src.startswith('data:image/png;base64,'))
                            data=base64.b64decode(icon.src.split(',',1)[1],validate=True)
                            name='provider-hub.png' if icon.theme=='light' else 'provider-hub-dark.png'
                            self.assertEqual(data,(ROOT/'assets'/name).read_bytes())
                            self.assertEqual(data[:8],b'\x89PNG\r\n\x1a\n')
                            self.assertEqual(struct.unpack('>II',data[16:24]),(128,128))
                            self.assertEqual(data[25],6)  # RGBA, with transparent background.


if __name__=='__main__':unittest.main()
