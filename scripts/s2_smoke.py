"""Exercise an isolated S2 fixture service; never point at production."""
import argparse
import io
import time
import zipfile
import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('url'); parser.add_argument('--verify-existing', action='store_true')
    arguments=parser.parse_args()
    with httpx.Client(base_url=arguments.url, timeout=20, trust_env=False) as client:
        for _ in range(40):
            try:
                health=client.get('/health'); health.raise_for_status()
                if health.json()['mode']=='core' and health.json()['fixture']: break
            except (httpx.HTTPError, KeyError): pass
            time.sleep(.5)
        else: raise RuntimeError('S2 fixture service did not become ready')
        def api(path, body=None):
            response=client.get('/api/v1/'+path) if body is None else client.post('/api/v1/'+path,json=body)
            response.raise_for_status(); return response.json()
        if not arguments.verify_existing:
            preview=api('subscriptions/preview', {})
            item=api('subscriptions', dict(preview_id=preview['id'], fail_page=2))
            job=api('subscriptions/'+item['id']+'/run', {})
            for _ in range(120):
                failed=next(j for j in api('tasks') if j['id']==job['id'])
                if failed['status'] in ('failed','completed'): break
                time.sleep(.1)
            assert failed['status']=='failed' and failed['chapters_done']==0
            assert not api('artifacts'), 'Failure produced a CBZ'
            retry=api('tasks/'+job['id']+'/retry', {})
            assert api('tasks/'+job['id']+'/retry', {})['id']==retry['id'], 'Retry created duplicate jobs'
            for _ in range(120):
                completed=next(j for j in api('tasks') if j['id']==retry['id'])
                if completed['status'] in ('failed','completed'): break
                time.sleep(.1)
            assert completed['status']=='completed' and completed['chapters_done']==2
            assert completed['reused_images']==1
        else:
            assert any(j['status']=='completed' and j['chapters_done']==2 for j in api('tasks'))
        artifacts=api('artifacts')
        assert len(artifacts)==2
        for artifact in artifacts:
            response=client.get('/api/v1/artifacts/'+artifact['id']); response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert archive.testzip() is None
                assert len([n for n in archive.namelist() if n.endswith('.png')])==3
        print('S2 smoke passed: persisted status, 2 chapters, 6 valid images, valid CBZ files' if arguments.verify_existing else
              'S2 smoke passed: partial failure, no false completion, idempotent retry, 1 reused page, 2 valid CBZ files')


if __name__=='__main__': main()
