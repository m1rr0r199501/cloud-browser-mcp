"""Ref identity and frame discovery contracts; live browser checks are explicit."""
from contextlib import asynccontextmanager
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cbmcp.errors import RefNotFound
from cbmcp.refs import SNAPSHOT_JS
from cbmcp.scope import ScopeRule
from cbmcp.session import BrowserSession


def test_actual_snapshot_js_keeps_identity_without_cross_document_or_clone_collisions():
    if not shutil.which('node'):
        pytest.skip('Node is optional for this offline JS fixture; live cloud checks cover actual Chromium')
    script = r"""const vm=require('node:vm'); const source=SOURCE;
class El {
 constructor(tag,text='',children=[],attrs={}){this.nodeType=1;this.tagName=tag;this.attrs={...attrs};this.textContent=text;this.childNodes=children.length?children:text?[{nodeType:3,nodeValue:text}]:[];}
 getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;}
 setAttribute(k,v){this.attrs[k]=String(v);}
 hasAttribute(k){return Object.hasOwn(this.attrs,k);}
 closest(){return null;}
 getBoundingClientRect(){return {width:100,height:20};}
}
function doc(prefix){const button=new El('BUTTON','target');const body=new El('BODY','',[button]);
const ctx=vm.createContext({window:{getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})},document:{body,title:'fixture',readyState:'complete'},location:{href:'https://fixture.invalid/'},opts:{refAttr:'data-fixture',seqKey:'_fixture',refPrefix:prefix}});
return {ctx,button,body};}
function snap(d,p){d.ctx.opts.refPrefix=p;return vm.runInContext('('+source+')(opts)',d.ctx);}
const a=doc('1'),b=doc('2');snap(a,'1');snap(b,'2');const first=a.button.attrs['data-fixture'];
snap(a,'99');const stable=a.button.attrs['data-fixture']===first;
const clone=new El('BUTTON','clone',[],a.button.attrs);a.body.childNodes.push(clone);snap(a,'100');
const fresh=doc('3');snap(fresh,'3');
const legacy=doc('4');legacy.ctx.window._fixture=0;legacy.button.attrs['data-fixture']='e1';snap(legacy,'4');
process.stdout.write(JSON.stringify({a:first,b:b.button.attrs['data-fixture'],stable,clone:clone.attrs['data-fixture'],fresh:fresh.button.attrs['data-fixture'],legacy:legacy.button.attrs['data-fixture']}));
""".replace('SOURCE', json.dumps(SNAPSHOT_JS))
    r = json.loads(subprocess.run(['node','-e',script], capture_output=True,text=True,check=True).stdout)
    assert len({r['a'],r['b'],r['clone'],r['fresh'],r['legacy']}) == 5
    assert r['stable'] and r['legacy'] != 'e1'


class Locator:
    def __init__(self, count, label):
        self.amount=count;self.label=label;self.first=self
    async def count(self):return self.amount


class Frame:
    def __init__(self, prefix, refs=None, *, parent=None, name=''):
        self.prefix=prefix;self.refs=refs or {};self.parent_frame=parent
        self.url='https://'+(name or prefix)+'.fixture.invalid/';self.name=name
    async def evaluate(self, expression, argument):return self.prefix
    def locator(self, selector):
        ref=selector.split('="',1)[1][:-2]
        return Locator(self.refs.get(ref,0),self.name)
    def is_detached(self):return False


@pytest.fixture
def session(settings,tmp_path):
    return BrowserSession(lease_id='fixture',settings=settings,playwright=None,
                          scope=ScopeRule(mode='off'),work_dir=tmp_path)


def handle(*frames):
    return SimpleNamespace(id='p1',debugger=None,cdp=None,page=SimpleNamespace(main_frame=frames[0],frames=list(frames)))


async def test_resolver_routes_to_real_document_and_recovers_registry_after_reconnect(session):
    main=Frame('1',{'e1_1':1},name='main');child=Frame('2',{'e2_1':1},parent=main,name='child')
    page=handle(main,child)
    assert (await session.resolve_ref(page,'e2_1')).label=='child'
    session._ref_doc_frames.clear()
    assert (await session.resolve_ref(page,'e2_1')).label=='child'
    child.prefix='3'  # navigation under the same Frame object
    with pytest.raises(RefNotFound):await session.resolve_ref(page,'e2_1')


async def test_legacy_collision_or_cloned_attribute_fails_without_choosing_first(session):
    main=Frame('1',{'e1':1,'e1_1':2},name='main');child=Frame('2',{'e1':1},parent=main,name='child')
    page=handle(main,child)
    with pytest.raises(RefNotFound,match='ambiguous'):await session.resolve_ref(page,'e1')
    with pytest.raises(RefNotFound,match='ambiguous'):await session.resolve_ref(page,'e1_1')


async def test_legacy_reference_is_not_assumed_unique_when_other_frame_cannot_be_read(session):
    from playwright.async_api import Error
    main=Frame('1',{'e1':1},name='main');bad=Frame('2',parent=main,name='bad')
    def unavailable(selector):raise Error('synthetic inaccessible frame')
    bad.locator=unavailable
    with pytest.raises(RefNotFound,match='unreadable'):await session.resolve_ref(handle(main,bad),'e1')


async def test_detached_frame_reference_is_not_retargeted_to_another_frame(session):
    main=Frame('1',{},name='main');old=Frame('2',{'e2_1':1},parent=main,name='old')
    session._remember_ref_frame('2',old)
    replacement=Frame('3',{'e2_1':1},parent=main,name='replacement')
    with pytest.raises(RefNotFound):await session.resolve_ref(handle(main,replacement),'e2_1')


async def test_child_detail_declares_local_box_coordinates_and_no_main_listener_lookup(session):
    main=Frame('1',name='main');child=Frame('2',{'e2_1':1},parent=main,name='child')
    async def evaluate(expression,argument):
        return '2' if expression.startswith('key =>') else {'tag':'input','box':{'x':1,'y':2,'w':3,'h':4}}
    child.evaluate=evaluate
    result=await session.element_detail(handle(main,child),'e2_1')
    assert result['frame_url']==child.url and result['box_coordinate_space']=='frame_viewport'
    assert 'event_listeners_note' in result and 'event_listeners' not in result


async def test_directory_is_paged_includes_empty_frames_and_does_not_read_bodies(session):
    main=Frame('1',name='main');children=[Frame(str(i+2),parent=main,name='f'+str(i)) for i in range(12)]
    session._frame_element_ref=AsyncMock(side_effect=lambda f:'iframe-'+f.name)
    session._snapshot_frame=AsyncMock()
    first=await session.snapshot(handle(main,*children),frames_only=True)
    assert first['child_frame_total']==12 and len(first['frames'])==8
    assert first['frames_not_in_response']==4 and first['has_more_frames']
    second=await session.snapshot(handle(main,*children),frames_only=True,frame_offset=first['next_frame_offset'])
    assert len(second['frames'])==4 and second['next_frame_offset'] is None
    assert {r['url'] for r in first['frames']+second['frames']}=={f.url for f in children}
    session._snapshot_frame.assert_not_awaited()


async def test_child_empty_and_failure_are_returned_as_distinct_rows(session):
    main=Frame('1',name='main');empty=Frame('2',parent=main,name='empty');bad=Frame('3',parent=main,name='bad')
    session._frame_element_ref=AsyncMock(return_value='iframe-ref')
    async def snapshot(frame,**kwargs):
        if frame==bad:raise TimeoutError()
        return {'url':frame.url,'lines':'root' if frame==main else '', 'nodes':1,'ref_prefix':frame.prefix}
    session._snapshot_frame=snapshot
    result=await session.snapshot(handle(main,empty,bad))
    assert [r['status'] for r in result['frames']]==['empty','error']
    assert result['frames'][1]['error']=='frame_snapshot_failed'


async def test_actual_snapshot_tool_exposes_frame_metadata_and_bounds_combined_frame_text(monkeypatch):
    from mcp.server.mcpserver import MCPServer
    from cbmcp.tools import inspect_tools
    mcp=MCPServer('fixture');inspect_tools.register(mcp)
    snap={'url':'https://fixture.invalid','lines':'root','nodes':1,'root_frame_is_main':True,
          'frames':[{'url':'https://child.invalid','frame_ref':'e1_3','lines':'x'*180,'status':'ok','nodes':1},
                    {'url':'https://child2.invalid','frame_ref':'e1_4','lines':'y'*180,'status':'ok','nodes':1}],
          'child_frame_total':12,'frame_offset':0,'next_frame_offset':2,'has_more_frames':True,'frames_not_in_response':10}
    fake=SimpleNamespace(handle=lambda p:SimpleNamespace(id='p1'),snapshot=AsyncMock(return_value=snap))
    @asynccontextmanager
    async def lease(*args,**kwargs):yield None,fake
    monkeypatch.setattr(inspect_tools,'use_lease',lease)
    monkeypatch.setattr(inspect_tools,'get_app',lambda:SimpleNamespace(s=SimpleNamespace(max_snapshot_chars=200)))
    monkeypatch.setattr(inspect_tools,'page_condition_flags',lambda *a,**k:{})
    result=await mcp.call_tool('page_snapshot',{'lease':'fixture','frame_limit':2})
    if isinstance(result,tuple):result=result[1]
    if isinstance(result,dict):data=result
    else:
        structured=getattr(result,'structuredContent',None)
        if structured:data=structured
        else:data=json.loads(''.join(c.text for c in getattr(result,'content',result) if c.type=='text'))
    assert data['child_frame_total']==12 and data['next_frame_offset']==2
    assert sum(len(r.get('snapshot','')) for r in data['frames'])<=200
    assert data['frames'][1]['truncated']
