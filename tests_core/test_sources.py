import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse
import httpx
from PIL import Image
from fastapi.testclient import TestClient
from download_core.api import create_app
from download_core.sources import real_sources, HipSession, CopySession, decode_hip
from download_core.transport import Transport, Budget
from download_core.models import CoreError

VECTOR='qM9A3G3MnI3VxIq5TB3OzpL0kM2v0B-Z7'


class SourceTests(unittest.TestCase):
    def setUp(self):
        stream=io.BytesIO(); Image.new('RGB',(12,20),'teal').save(stream,'PNG'); self.png=stream.getvalue()
        self.calls=[]

    def respond(self,request):
        self.calls.append(request)
        path=request.url.path
        if path.endswith('.png'): return httpx.Response(200,content=self.png)
        if path.startswith('/chapter/'):
            return httpx.Response(200,text='<div id="chapcontent" data-api-hid="ch1" data-api-base-url="https://hipapi1.s3file.top" data-chapter-img-base-line1="https://hip-tx-1.s3imgs.top"></div>')
        data={
            '/v1/mangas':dict(items=[dict(mid='demo-title',title='示例漫画',vertical_image_url='/cover.webp')],total_pages=2,total=2),
            '/v1/search':dict(data=[dict(id='demo',title='示例漫画')],total_pages=1,total=1),
            '/v1/manga':dict(title='示例漫画',description='测试简介',authors=[dict(name='作者')],status='ongoing'),
            '/v1/manga/chapters':dict(items=[dict(hid='ch1',title='第一章'),dict(hid='ch2',title='第二章')],total=2,total_pages=1),
            '/v2/chapter':dict(images=VECTOR)
        }.get(path)
        return httpx.Response(200,json=dict(code=200,data=data)) if data is not None else httpx.Response(404)

    def transport(self,source,handler=None):
        return Transport(httpx.Client(transport=httpx.MockTransport(handler or self.respond)),api_rate=0,image_rate=0,
                         scope=object(),auth_hosts=(urlparse(source.config['api_url']).hostname,))

    def test_hip_browse_search_catalog_and_manifest(self):
        source=real_sources()[0]; session=source.open_session(transport=self.transport(source))
        try:
            self.assertTrue(session.browse()['has_more'])
            self.assertEqual(session.browse(page=2)['page'],2)
            self.assertEqual(session.browse('示例')['items'][0]['comic_id'],'demo')
            self.assertEqual(session.get_comic('demo')['description'],'测试简介')
            page=session.list_chapters('demo','default')
            self.assertEqual([c.ordinal for c in page['items']],[1,2])
            manifest=session.get_manifest(page['items'][0])
            self.assertEqual(len(manifest.entries),2)
            self.assertTrue(manifest.entries[0].url.endswith('/a.png'))
            self.assertEqual(decode_hip(VECTOR),['a.png','b.png'])
            with self.assertRaises(CoreError): decode_hip('bad')
            with self.assertRaises(CoreError): session.browse(status='1')
        finally:session.close()

    def test_settings_redaction_and_origin_change(self):
        source=real_sources()[1]
        source.configure(dict(password='test-password',token='test-token',proxy='http://user:pass@127.0.0.1:7890'))
        public=json.dumps(source.public_config())
        for secret in ('test-password','test-token','user:pass'):self.assertNotIn(secret,public)
        source.configure(dict(api_url='https://api.example.org'))
        self.assertFalse(source.config['password']);self.assertFalse(source.config['token'])
        for values in (dict(api_rate=-1),dict(api_url='http://api.example.org'),dict(unknown='value'),dict(password=123)):
            with self.assertRaises(CoreError): source.configure(values)

    def test_api_auth_never_follows_to_image_host_even_after_yield(self):
        captured=[]
        def respond(request):
            captured.append(request)
            if request.url.host=='api.example.org':return httpx.Response(302,headers={'Location':'https://cdn.example.org/p'})
            return httpx.Response(200,content=b'ok')
        transport=Transport(httpx.Client(transport=httpx.MockTransport(respond)),api_rate=12,image_rate=0,scope=object(),auth_hosts=('api.example.org',))
        budget=Budget()
        try:
            with self.assertRaises(CoreError):transport.get('https://api.example.org/p',budget,role='api',allowed_hosts=('api.example.org','cdn.example.org'),headers={'Authorization':'Token private'})
            transport.shared_due[(transport.scope,'api')]=0
            transport.get('https://api.example.org/p',budget,role='api',allowed_hosts=('api.example.org','cdn.example.org'),headers={'Authorization':'Token private'})
            self.assertNotIn('Authorization',captured[-1].headers)
        finally:transport.close()

    def test_copy_api_pagination_and_210_preserved(self):
        source=real_sources()[1]
        def response(request):
            path=request.url.path
            if path.endswith('/chapter2/ch1'):return httpx.Response(210)
            if '/chapter/' in path:return httpx.Response(200,text='<html>No public image list</html>')
            if path.endswith('/chapters'):
                return httpx.Response(200,json=dict(code=200,results=dict(list=[dict(uuid='ch1',name='第一章')],total=1)))
            return httpx.Response(200,json=dict(code=200,results=dict(comic=dict(path_word='demo',name='示例'),groups=dict(default=dict(name='正文')))))
        session=source.open_session(transport=self.transport(source,response))
        try:
            self.assertEqual(session.get_comic('demo')['title'],'示例')
            page=session.list_chapters('demo','default')
            self.assertTrue(page['end_reached'])
            with self.assertRaises(CoreError) as caught:session.get_manifest(page['items'][0])
            self.assertEqual(caught.exception.code,'SITE_RESTRICTED')
        finally:session.close()

    def test_api_subscription_cards_policy_and_complete_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with TestClient(create_app(root/'data',root/'downloads',root/'cbz',fixture=False)) as client:
                source=client.app.state.engine.registry.get('hipmh')
                source.open_session=lambda config=None,transport=None: HipSession(source,dict(source.config),self.transport(source))
                self.assertIn('我的订阅',client.get('/').text)
                self.assertEqual([s['source_id'] for s in client.get('/api/v1/sources').json()],['hipmh','copymanga'])
                p=client.post('/api/v1/subscriptions/preview',json=dict(source_id='hipmh',comic_id='demo')).json()
                item=client.post('/api/v1/subscriptions',json=dict(preview_id=p['id'])).json()
                self.assertEqual(item['title'],'示例漫画')
                job=client.post('/api/v1/subscriptions/'+item['id']+'/run').json()
                deadline=time.time()+5
                while time.time()<deadline:
                    tasks=client.get('/api/v1/tasks').json()
                    if tasks[-1]['status'] in ('completed','failed'):break
                    time.sleep(.03)
                self.assertEqual(tasks[-1]['status'],'completed')
                cards=client.get('/api/v1/subscriptions').json()
                self.assertEqual(cards[0]['verified_chapters'],2)
                future=client.post('/api/v1/subscriptions/preview',json=dict(source_id='hipmh',comic_id='demo',mode='future')).json()
                response=client.put('/api/v1/subscriptions/'+item['id']+'/policy',json=dict(preview_id=future['id']))
                self.assertEqual(response.status_code,200)
                self.assertEqual(client.patch('/api/v1/subscriptions/'+item['id'],json=dict(paused=True,interval_hours=48)).status_code,200)
                self.assertEqual(client.post('/api/v1/subscriptions/'+item['id']+'/run').status_code,400)
                source_id='copymanga'
                response=client.put('/api/v1/sources/'+source_id+'/settings',json=dict(values=dict(password='private-test-value')))
                self.assertEqual(response.status_code,200);self.assertNotIn('private-test-value',response.text)
                self.assertNotIn('private-test-value',client.get('/api/v1/sources').text)
            with TestClient(create_app(root/'data',root/'downloads',root/'cbz',fixture=False)) as client:
                self.assertEqual(client.get('/api/v1/subscriptions').json()[0]['verified_chapters'],2)
                source=client.app.state.engine.registry.get('copymanga')
                self.assertEqual(source.config['password'],'private-test-value')


if __name__=='__main__':unittest.main()
