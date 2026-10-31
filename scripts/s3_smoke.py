"""Bounded real-source check against an isolated S3 service, never production."""
import argparse
import hashlib
import io
import json
import time
import zipfile
import httpx
from PIL import Image


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('url');parser.add_argument('--source',default='hipmh');parser.add_argument('--comic',required=True)
    parser.add_argument('--query',default='');parser.add_argument('--download-one',action='store_true');parser.add_argument('--verify-existing',action='store_true')
    args=parser.parse_args()
    def output(**data):print(json.dumps(data,ensure_ascii=True),flush=True)
    with httpx.Client(base_url=args.url,timeout=240,trust_env=False) as client:
        def api(path,body=None,method=None):
            r=client.request(method or ('GET' if body is None else 'POST'),'/api/v1/'+path,json=body)
            if r.status_code!=200:
                value=r.json();raise RuntimeError(str(value.get('error'))+': '+str(value.get('message')))
            return r.json()
        if not args.verify_existing:
            first=api('browse?'+str(httpx.QueryParams(source_id=args.source,page=1)))
            second=api('browse?'+str(httpx.QueryParams(source_id=args.source,page=2)))
            assert first['items'] and second['items'] and [i['comic_id'] for i in first['items']]!=[i['comic_id'] for i in second['items']]
            found=api('browse?'+str(httpx.QueryParams(source_id=args.source,q=args.query))) if args.query else first
            output(step='browse_search_pagination',source=args.source,page1=len(first['items']),page2=len(second['items']),search_titles=[c['title'] for c in found['items']][:5])
            detail=api('comics/'+args.source+'/'+args.comic)
            output(step='detail',title=detail['title'],has_cover=bool(detail.get('cover')),has_description=bool(detail.get('description')),groups=len(detail['groups']),notice=detail.get('notice'))
            if not detail['groups']:raise RuntimeError('Source did not provide groups; no subscription or download was created')
            group=detail['groups'][0]['group_id']
            catalog=api('catalog?'+str(httpx.QueryParams(source_id=args.source,comic_id=args.comic,group_id=group)))
            assert catalog['items'];latest=catalog['items'][-1]
            output(step='complete_catalog',chapters=len(catalog['items']),last_title=latest['title'])
            if not args.download_one:return
            subscriptions=api('subscriptions')
            existing=next((s for s in subscriptions if s['source_id']==args.source and s['comic_id']==args.comic and s['group_id']==group),None)
            if existing:
                if existing['verified_chapters']:
                    output(step='already_verified',chapters=existing['verified_chapters']);return
                api('subscriptions/'+existing['id'],{'paused':False},'PATCH');item=existing
            else:
                preview=api('subscriptions/preview',dict(source_id=args.source,comic_id=args.comic,group_id=group,mode='from',anchor=latest['chapter_key']))
                assert preview['after_anchor']==[latest['chapter_key']], 'Test must cover one chapter only'
                item=api('subscriptions',dict(preview_id=preview['id']))
            job=api('subscriptions/'+item['id']+'/run',{})
            deadline=time.time()+1200;previous=None;stop_sent=False
            while time.time()<deadline:
                current=next(j for j in api('tasks') if j['id']==job['id'])
                state=(current['status'],current['phase'],current['images_done'],current.get('error_code'))
                if state!=previous:
                    output(step='download',status=state[0],phase=state[1],images_done=state[2],images_total=current['images_total'],chapters_done=current['chapters_done'],error=current.get('error'))
                    previous=state
                if current['images_total'] and not stop_sent:
                    api('tasks/'+job['id']+'/stop',{});stop_sent=True
                if current['status'] in ('completed','stopped','failed','blocked','recovery_required'):break
                time.sleep(2)
            api('subscriptions/'+item['id'],{'paused':True},'PATCH')
            assert current['chapters_done']==1 and current['status'] in ('completed','stopped'), 'Real chapter was not completed'
        artifacts=api('artifacts');assert artifacts,'No verified artifacts'
        verified=0
        for artifact in artifacts:
            r=client.get('/api/v1/artifacts/'+artifact['id']);r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
                assert archive.testzip() is None
                manifest=json.loads(archive.read('download-manifest.json'))
                if manifest['identity'][0]!=args.source or manifest['identity'][1]!=args.comic:continue
                for page in manifest['pages']:
                    raw=archive.read(page['name']);assert hashlib.sha256(raw).hexdigest()==page['sha256']
                    with Image.open(io.BytesIO(raw)) as image:image.load()
                verified+=1;output(step='verified_cbz',pages=len(manifest['pages']),bytes=len(r.content),artifact_id=artifact['id'])
        assert verified==1
        output(result='passed',source=args.source,verified_chapters=verified,after_restart=args.verify_existing)


if __name__=='__main__':main()
