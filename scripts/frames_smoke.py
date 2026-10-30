#!/usr/bin/env python3
"""Explicit paid-cloud iframe test. Synthetic CDP fixtures; no local browser/target."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import urlsplit

from recon_smoke import unpack
import cbmcp.config as gateway_config
from cbmcp.app import App, set_app
from cbmcp.config import Settings
from cbmcp.server import build_server


ORIGIN='https://frames-fixture.invalid'
CROSS='https://frames-child.invalid'


def controls(label, nested=False):
    return f'''<!doctype html><meta charset=utf-8><title>{label}</title>
<form><input id="entry" name="entry" aria-label="Input {label}">
<button type="button" aria-label="Button {label}" onclick="window.clicks++;document.querySelector('output').textContent='Clicks:'+window.clicks">Act</button></form>
<output>Clicks:0</output><script>window.clicks=0;</script>
{f'<iframe id="nested" name="nested" src="{ORIGIN}/nested"></iframe>' if nested else ''}'''


MAIN=controls('main')+''.join(
    f'<iframe id="frame-{i}" name="child-{i}" src="{CROSS if i%2==0 else ORIGIN}/{"empty" if i==11 else "child/"+str(i)}"></iframe>'
    for i in range(12))


def ref_for(snapshot, label):
    line=next(line for line in snapshot.splitlines() if json.dumps(label) in line)
    return re.search(r'\[ref=([^\]]+)\]',line).group(1)


async def run(key, output):
    root=Path(tempfile.mkdtemp(prefix='cloud-frames-smoke-',dir='/private/tmp'))
    settings=Settings(_env_file=None,BROWSER_USE_API_KEY=key,provider='browseruse',data_dir=root,
        owner_tag='frame-test-'+secrets.token_hex(6),max_concurrency=1,max_lease_seconds=600,
        idle_timeout_seconds=180,platform_timeout_minutes=12,release_grace_seconds=0,
        scope_mode='enforce',scope_allow=['frames-fixture.invalid','frames-child.invalid'],
        reconcile_on_startup=False,reconcile_stop_untagged=False,log_level='ERROR')
    gateway_config._settings=settings
    app=App(settings);set_app(app);mcp=build_server();logging.disable(logging.CRITICAL)
    started=time.monotonic();lease=platform_id=None
    report={'passed':False,'cloud':True,'controlled_fixture_only':True,'local_browser_launched':False,
            'real_target_requests':0,'checks':[],'sessions_stopped':[],'key_saved':False}

    def check(name, value):
        if not value:raise AssertionError(name)
        report['checks'].append(name);print('PASS '+name,flush=True)

    async def call(name,**args):
        result=unpack(await mcp.call_tool(name,args))
        if result.get('ok') is False:raise AssertionError(name+' failed: '+str(result.get('error')))
        return result

    async def failed_call(name,**args):return unpack(await mcp.call_tool(name,args))

    async def fixture(route):
        path=urlsplit(route.request.url).path
        if path=='/main':html=MAIN
        elif path=='/empty':html='<!doctype html><title>empty</title><body></body>'
        elif path=='/nested':html=controls('nested')
        else:
            label=path.rsplit('/',1)[-1]
            html=controls('child-'+label,nested=label=='0')
        await route.fulfill(status=200,content_type='text/html',body=html)

    try:
        await app.start()
        acquired=await call('browser_acquire',task_label='controlled-frame-test',
            idempotency_key='frames-'+secrets.token_hex(5),scope_allow=['frames-fixture.invalid','frames-child.invalid'],debug='full',solve_captchas=False)
        if acquired.get('status')=='queued':
            acquired=await call('browser_queue_status',ticket_id=acquired['ticket_id'],wait_seconds=60)
        lease=acquired.get('lease');check('real cloud lease ready',bool(lease))
        obj=app.P.resolve_token(lease);session=obj.session;platform_id=obj.platform_session_id
        await session.context.route(ORIGIN+'/**',fixture);await session.context.route(CROSS+'/**',fixture)
        await call('page_navigate',lease=lease,url=ORIGIN+'/main',wait_until='domcontentloaded')
        page=session.handle(None).page
        await page.wait_for_load_state('load',timeout=30_000)
        snap=await call('page_snapshot',lease=lease)
        check('default reports all13 child frames including nested/blank',snap['child_frame_total']==13)
        check('default reports eight selected and five omitted',len(snap['frames'])==8 and snap['frames_not_in_response']==5 and snap['has_more_frames'])
        main_input=ref_for(snap['snapshot'],'Input main');main_button=ref_for(snap['snapshot'],'Button main')
        seen=set(re.findall(r'\[ref=([^\]]+)\]',snap['snapshot']))
        for frame in snap['frames']:
            refs=set(re.findall(r'\[ref=([^\]]+)\]',frame.get('snapshot','')))
            check('default body references disjoint for '+frame['name'],not seen.intersection(refs))
            seen.update(refs)
        same=await call('page_snapshot',lease=lease)
        check('same-document control ref survives another snapshot',ref_for(same['snapshot'],'Input main')==main_input)
        directory=await call('page_snapshot',lease=lease,frames_only=True)
        check('directory-only does not return DOM bodies',directory['snapshot']=='' and all('snapshot' not in f for f in directory['frames']))
        rest=await call('page_snapshot',lease=lease,frames_only=True,frame_offset=directory['next_frame_offset'])
        rows=directory['frames']+rest['frames']
        check('all directory pages enumerate13 unique frames',len(rows)==13 and len({r['frame_ref'] for r in rows})==13 and not rest['has_more_frames'])
        child_row=next(r for r in rows if r['url']==CROSS+'/child/0')
        child_snap=await call('page_snapshot',lease=lease,frame_ref=child_row['frame_ref'])
        check('explicit iframe snapshot is the cross-origin child',child_snap['url']==CROSS+'/child/0' and not child_snap['root_frame_is_main'])
        child_input=ref_for(child_snap['snapshot'],'Input child-0');child_button=ref_for(child_snap['snapshot'],'Button child-0')
        check('main and child controls have different document prefixes',main_input.split('_')[0]!=child_input.split('_')[0])
        await call('page_type',lease=lease,ref=child_input,text='child value')
        detail=(await call('page_snapshot',lease=lease,ref=child_input))['element']
        check('typing changes only selected child input',detail['frame_url']==CROSS+'/child/0' and detail['form']['fields'][0]['value']=='child value')
        main_detail=(await call('page_snapshot',lease=lease,ref=main_input))['element']
        check('main input is unchanged',main_detail['form']['fields'][0]['value']=='')
        check('child detail does not borrow main debugger listeners','event_listeners_note' in detail and 'event_listeners' not in detail)
        await call('page_click',lease=lease,ref=child_button)
        child=next(f for f in page.frames if f.url==CROSS+'/child/0')
        check('click affects child but not main',await child.evaluate('window.clicks')==1 and await page.evaluate('window.clicks')==0)
        nested=await call('page_snapshot',lease=lease,frame_ref=child_snap['frames'][0]['frame_ref'])
        check('nested iframe can be selected through its actual container ref',nested['url']==ORIGIN+'/nested')
        await call('page_click',lease=lease,ref=ref_for(nested['snapshot'],'Button nested'))
        nested_frame=next(f for f in page.frames if f.url==ORIGIN+'/nested')
        check('nested button routes to nested document only',await nested_frame.evaluate('window.clicks')==1 and await child.evaluate('window.clicks')==1)
        later=next(r for r in rows if r['index']>=8 and '/child/' in r['url'])
        late_snap=await call('page_snapshot',lease=lease,frame_ref=later['frame_ref'])
        label='child-'+urlsplit(later['url']).path.rsplit('/',1)[-1]
        await call('page_type',lease=lease,ref=ref_for(late_snap['snapshot'],'Input '+label),text='beyond eight')
        late_detail=(await call('page_snapshot',lease=lease,ref=ref_for(late_snap['snapshot'],'Input '+label)))['element']
        check('a frame beyond the original eight is reachable',late_detail['form']['fields'][0]['value']=='beyond eight')
        blank=next(r for r in rows if r['url'].endswith('/empty'))
        blank_snap=await call('page_snapshot',lease=lease,frame_ref=blank['frame_ref'])
        check('blank iframe is not silently omitted',blank_snap['snapshot']=='' and blank_snap['url'].endswith('/empty'))
        await child.evaluate("() => {const a=document.querySelector('#entry'),b=a.cloneNode(true);b.id='clone';b.name='clonedEntry';b.setAttribute('aria-label','Clone child-0');a.form.appendChild(b);}")
        collision=await failed_call('page_type',lease=lease,ref=child_input,text='must not apply')
        check('cloned duplicate attribute refuses action',collision.get('error')=='ref_not_found')
        check('no input changed during rejected ambiguous action',await child.evaluate("Array.from(document.querySelectorAll('input')).every(e=>e.value==='child value')"))
        refreshed=await call('page_snapshot',lease=lease,frame_ref=child_row['frame_ref'])
        clone_ref=ref_for(refreshed['snapshot'],'Clone child-0')
        check('fresh snapshot gives clone new ref and keeps original',clone_ref!=child_input and ref_for(refreshed['snapshot'],'Input child-0')==child_input)
        await call('page_type',lease=lease,ref=clone_ref,text='clone only')
        check('clone receives its own typed value',await child.evaluate("document.querySelector('#clone').value==='clone only' && document.querySelector('#entry').value==='child value'"))
        await child.goto(CROSS+'/child/replaced',wait_until='domcontentloaded')
        stale=await failed_call('page_type',lease=lease,ref=child_input,text='must not apply')
        check('document navigation invalidates old child ref',stale.get('error')=='ref_not_found')
        replaced=await call('page_snapshot',lease=lease,frame_ref=child_row['frame_ref'])
        replaced_ref=ref_for(replaced['snapshot'],'Input child-replaced')
        check('iframe container ref survives child navigation but control ref changes',replaced_ref!=child_input)
        await call('page_type',lease=lease,ref=replaced_ref,text='new child')
        check('replacement document gets the intended value',await child.evaluate("document.querySelector('#entry').value==='new child'"))
        await call('page_navigate',lease=lease,action='reload',wait_until='domcontentloaded')
        await page.wait_for_load_state('load',timeout=30_000)
        stale=await failed_call('page_click',lease=lease,ref=main_button)
        check('full page reload invalidates old main ref',stale.get('error')=='ref_not_found')
        fresh=await call('page_snapshot',lease=lease)
        fresh_input=ref_for(fresh['snapshot'],'Input main')
        check('full reload generates a fresh main document ref',fresh_input!=main_input)
        before=session.reconnects;session.liveness_failures=1
        check('same cloud browser reconnects',await app.P.reconnect(obj,why='controlled frames test'))
        session=obj.session
        check('actual fresh CDP connection established',session.reconnects==before+1)
        await call('page_type',lease=lease,ref=fresh_input,text='after reconnect')
        final=(await call('page_snapshot',lease=lease,ref=fresh_input))['element']
        check('ref routing recovers after reconnect without main-first guessing',final['form']['fields'][0]['value']=='after reconnect')
        report['passed']=True
    except Exception as exc:
        report['failure_type']=type(exc).__name__
        if isinstance(exc,AssertionError):report['failure_check']=str(exc).replace(key,'<redacted>')[:200]
        print('FAILED '+type(exc).__name__,flush=True)
        raise
    finally:
        try:
            if lease:
                try:await call('browser_release',lease=lease,reason='controlled frames test complete')
                except Exception:report['release_tool_failed']=True
            if platform_id:
                try:
                    stopped=await app.PR.confirm_stopped(platform_id)
                    if not stopped:
                        await app.PR.stop_session(platform_id)
                        stopped=await app.PR.confirm_stopped(platform_id)
                except Exception:stopped=False
                report['sessions_stopped'].append(stopped)
                if not stopped:report['passed']=False
                print('Platform session stopped: '+str(stopped),flush=True)
        finally:
            try:await app.stop()
            finally:
                set_app(None)
                report['duration_seconds']=round(time.monotonic()-started,2)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');os.chmod(output,0o600)
                print('Sanitized result: '+str(output),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();key=getpass.getpass('Browser Use key (not saved): ')
    try:asyncio.run(run(key,args.output))
    except Exception:raise SystemExit(1) from None
