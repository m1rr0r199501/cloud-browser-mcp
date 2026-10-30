function installRecon(P) {
  'use strict';
  const C = P.config;
  if (!C.origins.includes(location.origin) || globalThis[P.marker]) return;
  const marker = globalThis[P.marker] = {hash: P.hash, installed: true, at: 'document-start', errors: []};
  const cap = 65536;
  const clip = value => {
    const text = String(value == null ? '' : value);
    return {text: text.slice(0, cap), truncated: text.length > cap};
  };
  const emit = e => {
    e.observed_at = Date.now() / 1000;
    e.page_url = location.href;
    try { Promise.resolve(globalThis[P.binding](e)).catch(() => { marker.capture_failed = true; }); }
    catch (_) { marker.capture_failed = true; }
  };
  const absolute = value => { try { return new URL(value, location.href); } catch (_) { return null; } };
  const eligible = u => u && C.origins.includes(u.origin);
  const prefix = (u, paths) => paths.some(p => u.pathname.startsWith(p));
  const networkRule = (u, method) => eligible(u) && (C.network_rules || []).find(s =>
    s.path === u.pathname && (!s.method || s.method.toUpperCase() === method));
  const responseRule = (u, method) => {
    if (!eligible(u)) return null;
    const exact = C.stubs.find(s => s.path === u.pathname && (!s.method || s.method.toUpperCase() === method));
    if (exact) return {...exact, layer: 'L1'};
    if (C.mode === 'offline' && prefix(u, C.api_prefixes)) {
      const body = C.fallback_body !== undefined ? C.fallback_body :
        {code: 0, data: /\/(list|search|query|page|all|tree|nodes|options)(\/|$)/i.test(u.pathname) ? [] : {}};
      return {body, status: 200, layer: 'L3'};
    }
    return null;
  };
  // A selected request must reach the driver, even when L1/L3 would normally consume it.
  const rule = (u, method) => networkRule(u, method) ? null : responseRule(u, method);
  const driverMock = (u, method) => {
    const n = networkRule(u, method);
    return n && (n.error || responseRule(u, method));
  };
  const patch = (u, text) => {
    const n = C.neutralize;
    if (!eligible(u) || !n.paths || !prefix(u, n.paths)) return null;
    let body;
    try { body = JSON.parse(text); } catch (_) { return null; }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
    let changed = false;
    for (const path of n.fields) {
      const parts = path.split('.');
      let obj = body;
      for (const key of parts.slice(0, -1)) obj = obj && typeof obj === 'object' ? obj[key] : null;
      const key = parts[parts.length - 1];
      if (obj && typeof obj === 'object' && Object.hasOwn(obj, key) && n.failure_values.some(v => v === obj[key])) {
        obj[key] = n.success;
        changed = true;
      }
    }
    if (!changed) return null;
    Object.assign(body, n.flags || {});
    return JSON.stringify(body);
  };
  const headers = value => { try { return Object.fromEntries(new Headers(value).entries()); } catch (_) { return {}; } };
  const preview = body => {
    if (body == null) return {text: null, truncated: false};
    if (typeof body === 'string') return clip(body);
    if (body instanceof URLSearchParams) return clip(body.toString());
    if (body instanceof FormData) {
      return {fields: Array.from(body.entries()).slice(0, 200).map(([k,v]) => [k, typeof v === 'string' ? clip(v) :
        {file: true, name: v.name, type: v.type, size: v.size}]), original_bytes: false};
    }
    return {type: Object.prototype.toString.call(body), original_bytes: false};
  };
  for (const [name, values] of [['localStorage', C.local_storage], ['sessionStorage', C.session_storage]]) {
    try { for (const [key,value] of Object.entries(values || {})) globalThis[name].setItem(key, value); }
    catch (_) { marker.errors.push(name + '_unavailable'); }
  }
  const blocked = value => {
    const u = absolute(value);
    return eligible(u) && prefix(u, C.block_login_paths);
  };
  for (const method of ['pushState', 'replaceState']) {
    const raw = history[method];
    history[method] = function(state,title,url) { if (!blocked(url)) return raw.apply(this, arguments); };
  }
  // Location methods can be unforgeable. Do not claim that wrapping succeeded.
  for (const method of ['assign', 'replace']) {
    try {
      const raw = location[method].bind(location);
      const wrapped = function(url) { if (!blocked(url)) return raw(...arguments); };
      location[method] = wrapped;
      if (location[method] !== wrapped) marker.errors.push('location_' + method + '_not_wrapped');
    } catch (_) { marker.errors.push('location_' + method + '_not_wrapped'); }
  }

  const rawFetch = globalThis.fetch;
  globalThis.fetch = async function(input, init) {
    const req = input instanceof Request ? input : null;
    const u = absolute(req ? req.url : String(input));
    if (!eligible(u)) return rawFetch.apply(this, arguments);
    const method = String((init && init.method) || (req && req.method) || 'GET').toUpperCase();
    let body = init && Object.hasOwn(init, 'body') ? preview(init.body) : {text: null};
    if (req && !(init && Object.hasOwn(init, 'body'))) {
      try { body = clip(await req.clone().text()); } catch (_) { body = {unavailable: true}; }
    }
    const request = {url: u.href, method, headers: headers(init && init.headers || req && req.headers), body};
    const driverSynthetic = driverMock(u, method);
    const mock = rule(u, method);
    if (mock) {
      const text = JSON.stringify(mock.body);
      emit({transport: 'fetch', layer: mock.layer, request, request_sent: false,
        response: {status: mock.status || 200, body: clip(text)}, backend_verified: false});
      const result = new Response(text, {status: mock.status || 200, headers: {'Content-Type': 'application/json', ...mock.headers}});
      Object.defineProperty(result, 'url', {value: u.href});
      return result;
    }
    let response;
    try { response = await rawFetch.apply(this, arguments); }
    catch (error) {
      if (!driverSynthetic) emit({transport: 'fetch', layer: 'forward_error', request, request_sent: null,
        error: clip(error.message), backend_verified: false});
      throw error;
    }
    // The driver records its own abort/stub; do not label a fulfilled mock as an upstream response.
    if (driverSynthetic) return response;
    let text;
    try { text = await response.clone().text(); }
    catch (_) { text = null; }
    const patched = text === null ? null : patch(u, text);
    const upstream = {status: response.status, headers: headers(response.headers), body: text === null ? {unavailable: true} : clip(text),
      request_sent: true, representation: 'response_text', backend_verified: false};
    emit({transport: 'fetch', layer: patched === null ? 'forward' : 'L2', request, upstream,
      request_sent: true, response: {status: patched === null ? response.status : C.neutralize.http_status || response.status,
        body: patched === null ? null : clip(patched)}, backend_verified: false});
    if (patched === null) return response;
    const h = new Headers(response.headers);
    h.delete('content-length'); h.delete('content-encoding');
    const replacement = new Response(patched, {status: C.neutralize.http_status || response.status, headers: h});
    for (const key of ['url', 'redirected', 'type']) {
      try { Object.defineProperty(replacement, key, {value: response[key]}); } catch (_) {}
    }
    return replacement;
  };

  const X = XMLHttpRequest.prototype;
  const raw = {open: X.open, send: X.send, abort: X.abort, setRequestHeader: X.setRequestHeader};
  const descriptors = Object.fromEntries(['response', 'responseText', 'status', 'statusText', 'responseURL', 'readyState'].map(k => [k, Object.getOwnPropertyDescriptor(X,k)]));
  const state = new WeakMap();
  const native = (xhr,key) => descriptors[key].get.call(xhr);
  const override = (xhr,key,get) => Object.defineProperty(xhr,key,{configurable: true,get});
  const signal = (xhr,name) => xhr.dispatchEvent(new ProgressEvent(name));
  X.open = function(method,url,async = true) {
    for (const key of [...Object.keys(descriptors), 'getResponseHeader', 'getAllResponseHeaders']) {
      if (Object.hasOwn(this,key)) delete this[key];
    }
    const s = {url: absolute(url), method: String(method).toUpperCase(), headers: {}, async, sent: false};
    state.set(this,s);
    if (!this.__cbmcpReconObserved) {
      Object.defineProperty(this,'__cbmcpReconObserved',{value:true});
      this.addEventListener('readystatechange', () => {
        const current = state.get(this);
        if (!current || current.synthetic || current.driverMock || current.recorded || native(this,'readyState') !== 4 || !eligible(current.url)) return;
        current.recorded = true;
        let text = null;
        const json = this.responseType === 'json';
        try { text = json ? JSON.stringify(native(this,'response')) : native(this,'responseText'); } catch (_) {}
        const status = native(this,'status');
        const patched = text === null ? null : patch(current.url,text);
        const upstream = {status, headers: clip(this.getAllResponseHeaders()), body: text === null ? {unavailable:true} : clip(text),
          request_sent: status ? true : null, representation: json ? 'parsed_json_reserialized' : 'response_text', backend_verified:false};
        if (patched !== null) {
          override(this,'responseText',() => {
            if (this.responseType && this.responseType !== 'text') throw new DOMException('responseText unavailable','InvalidStateError');
            return patched;
          });
          const object = JSON.parse(patched);
          override(this,'response',() => json ? object : patched);
          if (C.neutralize.http_status) override(this,'status',() => C.neutralize.http_status);
        }
        emit({transport:'xhr', layer:patched === null ? 'forward' : 'L2', request:current.request,
          request_sent: status ? true : null, upstream, response:{status:this.status,body:patched === null ? null : clip(patched)}, backend_verified:false});
      },true);
    }
    return raw.open.apply(this,arguments);
  };
  X.setRequestHeader = function(name,value) {
    const s = state.get(this);
    if (s) s.headers[name] = s.headers[name] ? s.headers[name] + ', ' + value : String(value);
    return raw.setRequestHeader.apply(this,arguments);
  };
  X.send = function(body) {
    const s = state.get(this);
    if (!s || !eligible(s.url)) return raw.send.apply(this,arguments);
    s.request = {url:s.url.href,method:s.method,headers:s.headers,body:preview(body)};
    s.driverMock = !!driverMock(s.url, s.method);
    const mock = rule(s.url,s.method);
    if (!mock) return raw.send.apply(this,arguments);
    s.synthetic = true;
    const text = JSON.stringify(mock.body), object = mock.body;
    let ready = 1;
    const h = new Headers({'Content-Type':'application/json',...mock.headers});
    override(this,'readyState',() => ready);
    override(this,'status',() => ready >= 2 ? mock.status || 200 : 0);
    override(this,'statusText',() => ready >= 2 ? 'Mock' : '');
    override(this,'responseURL',() => s.url.href);
    override(this,'responseText',() => {
      if (this.responseType && this.responseType !== 'text') throw new DOMException('responseText unavailable','InvalidStateError');
      return ready >= 3 ? text : '';
    });
    override(this,'response',() => ready === 4 ? this.responseType === 'json' ? object :
      this.responseType === 'arraybuffer' ? new TextEncoder().encode(text).buffer :
      this.responseType === 'blob' ? new Blob([text],{type:'application/json'}) : text : null);
    this.getResponseHeader = name => ready >= 2 ? h.get(name) : null;
    this.getAllResponseHeaders = () => ready >= 2 ? Array.from(h.entries()).map(([k,v]) => k+': '+v+'\r\n').join('') : '';
    signal(this,'loadstart');
    const finish = () => {
      if (s.aborted || state.get(this) !== s) return;
      for (const n of [2,3,4]) { if (s.aborted) return; ready = n; signal(this,'readystatechange'); }
      emit({transport:'xhr',layer:mock.layer,request:s.request,request_sent:false,
        response:{status:mock.status || 200,body:clip(text)},backend_verified:false});
      signal(this,'load'); signal(this,'loadend');
    };
    if (s.async === false) finish(); else setTimeout(finish,0);
  };
  X.abort = function() {
    const s = state.get(this);
    if (!s || !s.synthetic) return raw.abort.apply(this,arguments);
    s.aborted = true;
    override(this,'readyState',() => 0); override(this,'status',() => 0);
    signal(this,'abort'); signal(this,'loadend');
  };
}
