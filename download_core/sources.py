"""Built-in site adapters. The core retains HTTP, file and completion ownership."""
import ast
import base64
import copy
import json
import os
import re
import secrets
import time
import threading
from collections import OrderedDict
from urllib.parse import urlencode, urljoin, urlparse
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from .models import Chapter, CoreError, ImageEntry, Manifest, digest, canonical
from .transport import Budget, Transport

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/137.0.0.0 Safari/537.36'


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value):
        raise CoreError('PROTOCOL_ERROR', '源返回了无效内容标识')
    return value


def https_url(value):
    parsed=urlparse(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CoreError('CONFIG_INVALID', '源地址必须是完整 HTTPS 地址，不能包含账号、查询参数或片段')
    return value.rstrip('/')


def names(values):
    return [v.get('name','') if isinstance(v,dict) else str(v) for v in (values or [])]


def decrypt(value, key):
    try:
        decryptor=Cipher(algorithms.AES(key.encode()),modes.CBC(value[:16].encode())).decryptor()
        raw=decryptor.update(bytes.fromhex(value[16:]))+decryptor.finalize()
        unpadder=PKCS7(128).unpadder()
        return json.loads(unpadder.update(raw)+unpadder.finalize())
    except (ValueError,TypeError,AttributeError):
        raise CoreError('PROTOCOL_ERROR','官网加密数据无法解析，网站格式可能已变化') from None


def decode_hip(value):
    try:
        if not value.startswith('qM9') or not value.endswith('Z7'): raise ValueError()
        body=value[3:-2]; length=len(body)-5; last=length//3; first=(length-last)//2; middle=length-last-first
        if body[first:first+2]!='Vx' or body[first+2+middle:first+5+middle]!='pL0': raise ValueError()
        reordered=body[first+5+middle:]+body[:first]+body[first+2:first+2+middle]
        unshuffled=''.join(reordered[i:i+7] if i//7%2==0 else reordered[i:i+7][::-1] for i in range(0,len(reordered),7))
        encoded=unshuffled.translate(str.maketrans('_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ','ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'))
        result=json.loads(base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)))
        if not isinstance(result,list) or not result or any(not isinstance(i,str) or not i for i in result): raise ValueError()
        return result
    except (ValueError,TypeError,AttributeError):
        raise CoreError('PROTOCOL_ERROR','嬉皮图片清单解析失败，未开始下载') from None


class SiteSource:
    plugin_version='0.1.0'
    source_api_major=1
    def __init__(self, source_id, title, defaults, session_type):
        self.source_id,self.title,self.defaults,self.session_type=source_id,title,defaults,session_type
        self.identity_scheme=source_id+'-v1'
        self.config=copy.deepcopy(defaults); self.generation=1
        self.token_cache=None; self.auth_lock=threading.RLock()
        self.comic_cache={}; self.cover_cache=OrderedDict(); self.cover_bytes=0; self.cover_lock=threading.RLock()

    def configure(self, values):
        unknown=set(values)-set(self.defaults)-{'clear_secrets'}
        if unknown: raise CoreError('CONFIG_INVALID','包含不支持的源配置字段')
        for field,value in values.items():
            if field=='clear_secrets':
                if not isinstance(value,list) or any(not isinstance(v,str) for v in value): raise CoreError('CONFIG_INVALID','清除项格式无效')
            elif field not in ('api_rate','image_rate') and (not isinstance(value,str) or len(value)>4096):
                raise CoreError('CONFIG_INVALID','配置文本无效或过长')
        candidate=dict(self.config)
        for field,value in values.items():
            if field=='clear_secrets': continue
            if field in ('password','token','proxy') and value=='': continue
            candidate[field]=value
        if candidate['api_url']!=self.config['api_url']:
            candidate.update(username='',password='',token='')
            for field in ('username','password','token'):
                if values.get(field): candidate[field]=values[field]
        for field in values.get('clear_secrets',[]):
            if field not in ('password','token','proxy','username'): raise CoreError('CONFIG_INVALID','不能清除这个字段')
            candidate[field]=''
        for field in ('api_url','web_url','reader_url','cover_url'):
            if field in candidate: candidate[field]=https_url(candidate[field])
        if candidate.get('proxy'):
            proxy=urlparse(candidate['proxy'])
            if proxy.scheme not in ('http','https') or not proxy.hostname: raise CoreError('CONFIG_INVALID','代理请填写 http://地址:端口，可包含代理认证')
        for field in ('api_rate','image_rate'):
            if type(candidate[field]) is not int or not 0<=candidate[field]<=600: raise CoreError('CONFIG_INVALID','限流需为 0 到 600 的整数，0 为关闭')
        if candidate.get('image_line','line1') not in ('line1','line2'): raise CoreError('CONFIG_INVALID','图片线路无效')
        self.config=candidate; self.generation+=1; self.token_cache=None
        self.comic_cache.clear();self.cover_cache.clear();self.cover_bytes=0

    def public_config(self):
        return {**{k:v for k,v in self.config.items() if k not in ('password','token','proxy')},
                **{'has_'+k:bool(self.config.get(k)) for k in ('password','token','proxy')},'generation':self.generation}

    def descriptor(self):
        return dict(source_id=self.source_id,display_name=self.title,hidden=False,plugin_version=self.plugin_version,
                    identity_scheme=self.identity_scheme,source_api_major=1,
                    capabilities=dict(browse=True,search=True,groups=True,filters=True,anonymous=True,account_required=False),
                    settings=self.public_config())

    def open_session(self, config=None, transport=None):
        return self.session_type(self,dict(self.config),transport)


class Session:
    def __init__(self,source,config,transport=None):
        self.source,self.source_id,self.config=source,source.source_id,config
        self.api,self.web=config['api_url'],config['web_url']
        self.api_host=urlparse(self.api).hostname
        self.allowed={urlparse(value).hostname for name,value in config.items() if name.endswith('_url')}
        self.transport=transport or Transport(proxy=config.get('proxy'),api_rate=config['api_rate'],image_rate=config['image_rate'],
                                            scope=(source.source_id,config.get('username','')),auth_hosts=(self.api_host,))
        self.budgets={}; self.cache={}; self.image_hosts=set(); self.image_headers={'User-Agent':UA,'Referer':self.web+'/'}
        self.refreshed=False

    def raw(self,url,headers=None,method='GET',data=None):
        budget=self.budgets.setdefault((method,url),Budget())
        while True:
            try: return self.transport.request(url,budget,role='api',allowed_hosts=self.allowed,headers={'User-Agent':UA,**(headers or {})},method=method,data=data)
            except CoreError as error:
                # Pace local API calls, but yield long server cooldowns to the worker/UI.
                if error.code=='RATE_LIMIT' and error.retry_at and 0<error.retry_at-time.time()<=5.05 and budget.used<budget.maximum:
                    time.sleep(max(0,error.retry_at-time.time())); continue
                raise

    def fetch(self,path,params=None,headers=None,base=None):
        url=(base or self.api)+path+('?' + urlencode(params) if params else '')
        try:
            payload=json.loads(self.raw(url,headers))
            code=payload.get('code')
            if code==210: raise CoreError('SITE_RESTRICTED','CopyManga 返回 210：请求受到站点限制，未取得完整目录或图片')
            if code not in (None,200): raise CoreError('UPSTREAM_ERROR','源站返回失败，未将响应当成空列表')
            result=payload.get('data',payload.get('results'))
            if not isinstance(result,dict): raise ValueError()
            return result
        except (ValueError,TypeError,AttributeError):
            raise CoreError('PROTOCOL_ERROR','源站响应格式错误，未将请求失败当成无结果') from None

    def image_url(self,value,base):
        url=urljoin(base.rstrip('/')+'/',value)
        host=urlparse(url).hostname
        suffixes=('s3imgs.top','s3file.top','hipmh.top') if self.source_id=='hipmh' else ('copymanga.com','copymanga.org','mangacopy.com','copymanga.site','copy2000.online')
        if urlparse(url).scheme!='https' or not host or not any(host==s or host.endswith('.'+s) for s in suffixes):
            raise CoreError('PROTOCOL_ERROR','图片主机不在源允许的范围内，未发出请求')
        self.image_hosts.add(host)
        return url

    def close(self): self.transport.close()


class HipSession(Session):
    def comic(self,item):
        comic_id=identifier(str(item.get('mid') or item.get('id') or '').split('-')[0])
        cover=item.get('vertical_image_url') or item.get('cover_image_url') or ''
        return dict(comic_id=comic_id,title=item.get('title') or comic_id,description=item.get('description') or '',
                    cover=urljoin(self.config['cover_url']+'/',cover) if cover else '',authors=names(item.get('authors') or item.get('author_names')),
                    genres=names(item.get('genres')),status={'ongoing':'连载中','completed':'已完结'}.get(item.get('status'),item.get('status') or ''))

    def browse(self,query='',page=1,sort='updated',status='',theme=''):
        if sort not in ('updated','popular'): raise CoreError('CONFIG_INVALID','排序无效')
        if status or theme: raise CoreError('FILTER_UNSUPPORTED','嬉皮接口只提供热门/更新排序，不支持此筛选组合')
        data=self.fetch('/v1/search',dict(q=query.strip(),page=page,page_size=20)) if query.strip() else self.fetch('/v1/mangas',dict(sort=sort,page=page,per_page=20))
        items=data.get('data') if query.strip() else data.get('items')
        if not isinstance(items,list): raise CoreError('PROTOCOL_ERROR','嬉皮列表字段无效')
        return dict(items=[self.comic(i) for i in items],page=page,has_more=page<int(data.get('total_pages',page)),total=data.get('total'))

    def get_comic(self,comic_id):
        comic_id=identifier(comic_id)
        if comic_id not in self.cache:
            data=self.fetch('/v1/manga',{'mid':comic_id}); self.cache[comic_id]=self.comic(dict(data,id=comic_id))
        return self.cache[comic_id]

    def list_groups(self,comic_id):
        self.get_comic(comic_id); return [dict(group_id='default',name='正文')]

    def list_chapters(self,comic_id,group_id,cursor=None):
        if group_id!='default': raise CoreError('NOT_FOUND','分组不存在')
        page=int(cursor or 1)
        data=self.fetch('/v1/manga/chapters',dict(mid=identifier(comic_id),page=page,per_page=50,order='asc'))
        items=data.get('items'); total=data.get('total'); pages=int(data.get('total_pages',1))
        if not isinstance(items,list) or type(total) is not int: raise CoreError('CATALOG_INCOMPLETE','嬉皮目录缺少数量或章节列表')
        chapters=[Chapter(self.source_id,comic_id,group_id,identifier(i['hid']),i.get('title') or '第'+str(i.get('chapter_number',''))+'章',
                          (page-1)*50+n+1,i.get('updated_at')) for n,i in enumerate(items)]
        return dict(items=chapters,total=total,snapshot_token=None,end_reached=page>=pages,next_cursor=str(page+1) if page<pages else None)

    def get_manifest(self,chapter,refresh=False):
        soup=BeautifulSoup(self.raw(self.config['reader_url']+'/chapter/'+identifier(chapter.chapter_id)),'html.parser')
        node=soup.select_one('#chapcontent')
        if node is None or not node.get('data-api-hid'): raise CoreError('PROTOCOL_ERROR','嬉皮章节配置缺失')
        base=node.get('data-api-base-url',self.api).rstrip('/')
        if base.endswith('/v1'): base=base[:-3]
        elif base.endswith('/v2/chapter'): base=base[:-11]
        elif base.endswith('/v2'): base=base[:-3]
        if urlparse(base).hostname not in self.allowed: raise CoreError('PROTOCOL_ERROR','章节 API 主机不在允许范围')
        data=self.fetch('/v2/chapter',{'hid':node['data-api-hid']},base=base)
        images=decode_hip(data['images'])
        if data.get('order_id') is not None and data.get('sid') is not None:
            index=int(data['order_id']) ^ (((int(data['sid'])*2654435761)^(len(images)*2246822507))%len(images))
            if not 0<=index<len(images): raise CoreError('PROTOCOL_ERROR','嬉皮图片位置校验失败')
            images.pop(index)
        attrs=['data-chapter-img-base-line1s','data-chapter-img-base-line2s','data-chapter-img-base'] if data.get('line')==9 else ['data-chapter-img-base-line1','data-chapter-img-base-line2','data-chapter-img-base']
        if self.config.get('image_line')=='line2': attrs[0],attrs[1]=attrs[1],attrs[0]
        host=next((node.get(attr) for attr in attrs if node.get(attr)),None)
        if not host: raise CoreError('PROTOCOL_ERROR','嬉皮图片地址缺失')
        self.image_headers['Referer']=self.config['reader_url']+'/'
        return Manifest(chapter,tuple(ImageEntry(i+1,str(i+1),self.image_url(url,host)) for i,url in enumerate(images)),expected_count=len(images)).validate()


class CopySession(Session):
    def auth_headers(self):
        token=self.config.get('token','')
        if not token and self.config.get('username') and self.config.get('password'):
            with self.source.auth_lock:
                cached=self.source.token_cache
                if cached and cached[0]==self.source.generation and cached[2]>time.time(): token=cached[1]
                else:
                    salt=str(secrets.randbelow(900000)+100000)
                    raw=self.raw(self.api+'/api/kb/web/login',method='POST',data={'username':self.config['username'],
                        'password':base64.b64encode((self.config['password']+'-'+salt).encode()).decode(),'salt':salt})
                    try:
                        token=json.loads(raw)['results']['token']
                        if not isinstance(token,str) or not token: raise ValueError()
                    except (KeyError,ValueError,TypeError): raise CoreError('AUTH_REQUIRED','拷贝账号登录失败，未开始下载') from None
                    self.source.token_cache=(self.source.generation,token,time.time()+1800)
            self.config['token']=token
        return {'Authorization':token if token.startswith(('Token ','Bearer ')) else 'Token '+token} if token else {}

    def auth_fetch(self,path,params=None):
        try: return self.fetch(path,params,headers=self.auth_headers())
        except CoreError as error:
            if error.code!='AUTH_REQUIRED' or self.refreshed or not self.config.get('password'): raise
            self.refreshed=True; self.config['token']=''; self.source.token_cache=None
            return self.fetch(path,params,headers=self.auth_headers())

    def comic(self,item):
        item=item.get('comic',item); comic_id=identifier(item['path_word']); status=item.get('status')
        return dict(comic_id=comic_id,title=item.get('name') or comic_id,cover=item.get('cover') or '',description=item.get('brief') or '',
                    authors=names(item.get('author')),genres=names(item.get('theme')),status=status.get('display','') if isinstance(status,dict) else str(status or ''))

    def browse(self,query='',page=1,sort='updated',status='',theme=''):
        params=dict(limit=20,offset=(page-1)*20)
        if query.strip():
            if status or theme: raise CoreError('FILTER_UNSUPPORTED','拷贝搜索不支持分类和状态组合，请清空筛选后搜索')
            data=self.fetch('/api/kb/web/searchcl/comics',dict(params,q=query.strip(),q_type='',platform=2))
        else:
            params.update(ordering='-popular' if sort=='popular' else '-datetime_updated',**{k:v for k,v in dict(status=status,theme=theme).items() if v!=''})
            try: data=self.fetch('/api/v3/comics',params)
            except CoreError as error:
                if error.code not in ('NETWORK','SITE_RESTRICTED','HTTP_ERROR'): raise
                soup=BeautifulSoup(self.raw(self.web+'/comics?'+urlencode(params)),'html.parser')
                box=soup.select_one('.exemptComic-box[list][total]')
                if box is None: raise CoreError('PROTOCOL_ERROR','API 无法访问，官网列表也未能解析，请检查源地址和代理')
                try: data=dict(list=ast.literal_eval(box['list']),total=int(box['total']))
                except (ValueError,SyntaxError,TypeError): raise CoreError('PROTOCOL_ERROR','官网列表格式已变化') from None
        items=data.get('list'); total=data.get('total')
        if not isinstance(items,list): raise CoreError('PROTOCOL_ERROR','拷贝列表格式错误')
        return dict(items=[self.comic(i) for i in items],page=page,total=total,has_more=params['offset']+len(items)<total if type(total) is int else len(items)==20)

    def get_comic(self,comic_id):
        comic_id=identifier(comic_id)
        if comic_id not in self.cache:
            try:
                data=self.auth_fetch('/api/v3/comic2/'+comic_id)
                comic=self.comic(data.get('comic',data)); raw=data.get('groups')
                if not isinstance(raw,(dict,list)) or not raw: raise CoreError('CATALOG_INCOMPLETE','API 未提供分组')
                entries=raw.items() if isinstance(raw,dict) else [(g.get('path_word'),g) for g in raw]
                comic['groups']=[dict(group_id=identifier(g.get('path_word') or k),name=g.get('name') or k) for k,g in entries]
                self.cache[comic_id]=comic
            except CoreError as error:
                if error.code not in ('NETWORK','HTTP_ERROR','SITE_RESTRICTED','CATALOG_INCOMPLETE'): raise
                comic=self.web_comic(comic_id)
                comic['notice']='API 受限，已尝试官网公开资料；'+comic.get('notice','')
                self.cache[comic_id]=comic
        return self.cache[comic_id]

    def web_comic(self,comic_id):
        html=self.raw(self.web+'/comic/'+comic_id); soup=BeautifulSoup(html,'html.parser')
        title=soup.select_one('.comicParticulars-title-right h6')
        if title is None: raise CoreError('SITE_RESTRICTED','拷贝 API 受限，官网也未返回有效漫画资料')
        cover=soup.select_one('.comicParticulars-left-img img'); intro=soup.select_one('p.intro')
        comic=dict(comic_id=comic_id,title=title.get_text(strip=True),cover=urljoin(self.web,cover.get('data-src') or cover.get('src') or '') if cover else '',
                   description=intro.get_text(strip=True) if intro else '',authors=[],genres=[],status='',groups=[])
        match=re.search(r'\bvar\s+ccz\s*=\s*([\'"])(.*?)\1',html.decode('utf-8',errors='replace')); dnt=soup.select_one('#dnt[value]')
        if match and dnt:
            try:
                response=json.loads(self.raw(self.web+'/comicdetail/'+comic_id+'/chapters',{'dnts':dnt['value'],'Referer':self.web+'/comic/'+comic_id}))
                payload=decrypt(response['results'],match[2]); raw=payload['groups']
                if payload['build']['path_word']!=comic_id or not isinstance(raw,dict): raise ValueError()
                directories={}
                for group_id,group in raw.items():
                    identifier(group_id); chapters=group['chapters']
                    if not isinstance(chapters,list) or group.get('count',len(chapters))!=len(chapters): raise ValueError()
                    directories[group_id]=[Chapter(self.source_id,comic_id,group_id,identifier(c['id']),c['name'],i+1,c.get('datetime_created')) for i,c in enumerate(chapters)]
                    comic['groups'].append(dict(group_id=group_id,name=group.get('name') or group_id))
                if not any(directories.values()): raise ValueError()
                self.cache['dirs:'+comic_id]=directories
            except (CoreError,KeyError,ValueError,TypeError):
                comic['groups']=[]; comic['notice']='官网未取得完整目录，暂不能订阅。'
        else: comic['notice']='官网目录参数缺失，暂不能订阅。'
        return comic

    def list_groups(self,comic_id): return self.get_comic(comic_id)['groups']

    def list_chapters(self,comic_id,group_id,cursor=None):
        self.get_comic(comic_id); group_id=identifier(group_id)
        directories=self.cache.get('dirs:'+comic_id)
        if directories is not None:
            if not directories.get(group_id): raise CoreError('CATALOG_INCOMPLETE','官网目录为空，未按无更新处理')
            items=directories[group_id]
            return dict(items=items,total=len(items),snapshot_token=digest(canonical([c.data() for c in items]).encode()),end_reached=True,next_cursor=None)
        offset=int(cursor or 0)
        data=self.auth_fetch('/api/v3/comic/'+identifier(comic_id)+'/group/'+group_id+'/chapters',dict(limit=500,offset=offset))
        raw=data.get('list'); total=data.get('total')
        if not isinstance(raw,list) or type(total) is not int: raise CoreError('CATALOG_INCOMPLETE','拷贝目录格式错误')
        items=[Chapter(self.source_id,comic_id,group_id,identifier(c['uuid']),c['name'],offset+i+1,c.get('datetime_created')) for i,c in enumerate(raw)]
        end=offset+len(items)>=total
        return dict(items=items,total=total,snapshot_token=None,end_reached=end,next_cursor=str(offset+len(items)) if not end and items else None)

    def get_manifest(self,chapter,refresh=False):
        try:
            data=self.auth_fetch('/api/v3/comic/'+chapter.comic_id+'/chapter2/'+chapter.chapter_id)
            data=data.get('chapter',data); pages=data.get('contents'); words=data.get('words')
            if not isinstance(pages,list) or not pages: raise CoreError('PROTOCOL_ERROR','拷贝未返回完整图片清单')
            if words is not None:
                if len(words)!=len(pages) or len(set(words))!=len(words): raise CoreError('PROTOCOL_ERROR','拷贝图片页序无效')
                pages=[p for _,p in sorted(zip(words,pages))]
        except CoreError as error:
            if error.code!='SITE_RESTRICTED': raise
            html=self.raw(self.web+'/comic/'+chapter.comic_id+'/chapter/'+chapter.chapter_id).decode('utf-8',errors='replace')
            encrypted=re.search(r'\bvar\s+contentKey\s*=\s*([\'"])(.*?)\1',html)
            encryption_key=re.search(r'\bvar\s+cct\s*=\s*([\'"])(.*?)\1',html)
            if not encrypted or not encryption_key: raise CoreError('SITE_RESTRICTED','拷贝返回 210，官网也未取得完整图片清单，未下载或记为完成')
            pages=decrypt(encrypted[2],encryption_key[2])
        if not isinstance(pages,list) or not pages: raise CoreError('PROTOCOL_ERROR','图片清单为空')
        return Manifest(chapter,tuple(ImageEntry(i+1,str(i+1),self.image_url(p['url'],self.web)) for i,p in enumerate(pages)),expected_count=len(pages)).validate()


def real_sources():
    common=dict(proxy='',username='',password='',token='',image_rate=60)
    sources=[SiteSource('hipmh','嬉皮漫画',dict(common,api_url='https://hipapi1.s3file.top',web_url='https://www.hipmh.com',
                      reader_url='https://reader.hipmh.top',cover_url='https://cover.s3imgs.top',api_rate=180,image_line='line1'),HipSession),
            SiteSource('copymanga','拷贝漫画',dict(common,api_url='https://api.mangacopy.com',web_url='https://www.copy4000.com',api_rate=12),CopySession)]
    values={field:os.environ[variable] for field,variable in dict(api_url='CMNAS_API_URL',web_url='CMNAS_CM_WEB_URL',
            username='CMNAS_CM_USERNAME',password='CMNAS_CM_PASSWORD',token='CMNAS_CM_TOKEN',proxy='CMNAS_CM_PROXY').items() if os.environ.get(variable)}
    if 'CMNAS_CM_RATE_LIMIT_PER_MINUTE' in os.environ:
        try: values['api_rate']=int(os.environ['CMNAS_CM_RATE_LIMIT_PER_MINUTE'])
        except ValueError: raise CoreError('CONFIG_INVALID','CMNAS_CM_RATE_LIMIT_PER_MINUTE 必须是整数') from None
    if values: sources[1].configure(values)
    return sources
