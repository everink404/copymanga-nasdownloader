"""Offline source and injected HTTP data; never contacts a manga site."""
import io
import httpx
from PIL import Image, ImageDraw
from .models import Chapter, CoreError, ImageEntry, Manifest, digest
from .transport import Transport


class FixtureSource:
    source_id = 'fixture_offline'
    plugin_version = '0.1.0'
    identity_scheme = 'fixture-v1'
    source_api_major = 1

    def __init__(self):
        self.payloads = {}
        self.failures = {}
        for i in range(1, 4):
            image = Image.new('RGB', (320, 440), ('#bcd5df', '#d0dac0', '#ecd1b0')[i-1])
            ImageDraw.Draw(image).text((30, 200), f'Offline download test - page {i}', fill='black')
            stream = io.BytesIO(); image.save(stream, 'PNG')
            self.payloads[str(i)] = stream.getvalue()

    def descriptor(self):
        return dict(source_id=self.source_id, display_name='离线测试源', hidden=True,
                    plugin_version=self.plugin_version, identity_scheme=self.identity_scheme, source_api_major=1,
                    capabilities=dict(browse=True, search=True, groups=True, filters=False, anonymous=True,
                                      account_required=False, manifest_refresh=True), settings_schema=[],
                    endpoints={'fixture': ['fixture.invalid']}, rate_limits={'api_requests_per_minute': 0, 'image_requests_per_minute': 0})

    def open_session(self, config, transport=None):
        return FixtureSession(self, config, transport)


class FixtureSession:
    source_id = 'fixture_offline'

    def __init__(self, source, config, transport):
        self.source, self.config = source, config
        self.transport = transport or Transport(client=httpx.Client(transport=httpx.MockTransport(self.respond)), api_rate=0, image_rate=0, scope=('fixture', id(source)))

    def respond(self, request):
        page = request.url.path.rsplit('/', 1)[-1]
        if self.config.get('fail_page') == int(page) and not self.source.failures.get(self.config['failure_id']):
            self.source.failures[self.config['failure_id']] = True
            return httpx.Response(403, content=b'controlled failure')
        if self.config.get('html_page') == int(page):
            return httpx.Response(200, content=b'<html>not an image</html>')
        return httpx.Response(200, content=self.source.payloads[page], headers={'content-type': 'image/png'})

    def browse(self, query=''):
        items = [dict(comic_id='demo', title='离线下载示例', description='本地生成的三张测试图片，不连接漫画网站')]
        return [i for i in items if not query or query in i['title']]

    def get_comic(self, comic_id):
        if comic_id != 'demo': raise CoreError('NOT_FOUND', '测试作品不存在')
        return self.browse()[0]

    def list_groups(self, comic_id):
        self.get_comic(comic_id)
        return [dict(group_id='default', name='正文')]

    def list_chapters(self, comic_id, group_id, cursor=None):
        self.get_comic(comic_id)
        if group_id != 'default': raise CoreError('NOT_FOUND', '分组不存在')
        chapters = [Chapter(self.source_id, comic_id, group_id, str(i), f'第{i}章', i, '1') for i in range(1, 3)]
        page = int(cursor or 0)
        return dict(items=chapters[page:page+1], total=2, next_cursor=str(page+1) if page == 0 else None,
                    end_reached=page == 1, snapshot_token='fixture-catalog-1')

    def get_manifest(self, chapter, refresh=False):
        return Manifest(chapter, tuple(ImageEntry(i, f'{chapter.chapter_id}/{i}', f'https://fixture.invalid/{i}',
                                                  digest(self.source.payloads[str(i)])) for i in range(1, 4)),
                        proof_kind=self.config.get('proof_kind', 'content_hash'), expected_count=3).validate()

    def close(self):
        self.transport.close()


class Registry:
    def __init__(self, sources=()):
        self.sources = {}
        for source in sources:
            descriptor = source.descriptor()
            if descriptor['source_api_major'] != 1 or source.source_id in self.sources:
                raise CoreError('SOURCE_UNAVAILABLE', '漫画源版本不兼容或标识重复')
            self.sources[source.source_id] = source

    def get(self, source_id):
        source = self.sources.get(source_id)
        if source is None:
            raise CoreError('SOURCE_UNAVAILABLE', '该漫画源尚未启用，原数据保留')
        return source
