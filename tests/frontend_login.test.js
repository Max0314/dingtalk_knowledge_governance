const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/login.js'), 'utf8');

async function page(reply, url = 'https://example.invalid/knowledge_governance/login.html', loginReply = null) {
  const elements = new Map(['#login-button', '#retry-button', '#login-status'].map(id => [id, {
    disabled: true, hidden: true, textContent: '', classes: new Set(), listeners: {},
    classList: {toggle() {}}, addEventListener(event, fn) {this.listeners[event] = fn;}
  }]));
  const parsed = new URL(url);
  const replacements = [], requests = [], assigned = [];
  const context = vm.createContext({URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    document: {querySelector: id => elements.get(id)},
    location: {href: url, search: parsed.search, hash: parsed.hash,
      replace: value => replacements.push(value), assign: value => assigned.push(value)},
    fetch: async (target, options) => {
      requests.push({url: String(target), options});
      const response = String(target).includes('login-url') ? loginReply : reply;
      if (response instanceof Error) throw response;
      return {ok: response.status < 400, status: response.status, json: async () => response.data};
    }
  });
  vm.runInContext(code, context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, replacements, requests, assigned};
}

test('anonymous login only checks session, enables authorization, never fetches business data', async () => {
  const result = await page({status: 401, data: {}});
  assert.equal(result.elements.get('#login-button').disabled, false);
  assert.equal(result.requests.length, 1);
  assert.match(result.requests[0].url, /api\/auth\/me$/);
});
test('session check failure stays closed with retry instead of rendering workspace', async () => {
  const result = await page({status: 503, data: {}});
  assert.equal(result.elements.get('#login-button').disabled, true);
  assert.equal(result.elements.get('#retry-button').hidden, false);
  assert.deepEqual(result.replacements, []);
});
test('valid session restores the fragment preserved by an entry redirect', async () => {
  const result = await page({status: 200, data: {auth_enabled: true, user: {union_id: 'synthetic'}}},
    'https://example.invalid/knowledge_governance/login.html?return_url=%2Fknowledge_governance%2F#/reviews');
  assert.deepEqual(result.replacements, ['https://example.invalid/knowledge_governance/#/reviews']);
});
test('external return target cannot be used', async () => {
  const result = await page({status: 200, data: {auth_enabled: false}},
    'https://example.invalid/knowledge_governance/login.html?return_url=https%3A%2F%2Foutside.invalid');
  assert.deepEqual(result.replacements, ['https://example.invalid/knowledge_governance/#/overview']);
});
test('callback error is visible and authorization can be retried', async () => {
  const result = await page({status: 401, data: {}},
    'https://example.invalid/knowledge_governance/login.html?error=state_expired');
  assert.match(result.elements.get('#login-status').textContent, /已过期/);
  assert.equal(result.elements.get('#login-button').disabled, false);
});
test('authorization failure restores the button without unsafe navigation', async () => {
  const result = await page({status: 401, data: {}}, undefined,
    {status: 200, data: {login_url: 'https://outside.invalid/'}});
  await result.elements.get('#login-button').listeners.click();
  assert.equal(result.elements.get('#login-button').disabled, false);
  assert.deepEqual(result.assigned, []);
});
test('authorization uses only the official login host and keeps the deep link', async () => {
  const result = await page({status: 401, data: {}},
    'https://example.invalid/knowledge_governance/login.html#/increments',
    {status: 200, data: {login_url: 'https://login.dingtalk.com/oauth2/auth?state=synthetic'}});
  await result.elements.get('#login-button').listeners.click();
  assert.match(result.assigned[0], /^https:\/\/login.dingtalk.com\//);
  assert.equal(new URL(result.requests[1].url).searchParams.get('return_url'),
    'https://example.invalid/knowledge_governance/#/increments');
});

const workspaceCode = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
async function workspace(reply) {
  const replacements = [], rendered = [], requests = [];
  const context = vm.createContext({URL, AbortController, setTimeout, clearTimeout,
    document: {querySelector: () => ({innerHTML: '', classList: {toggle() {}}}),
      querySelectorAll: () => [], body: {classList: {remove() {}}}},
    window: {AbortController, addEventListener() {}},
    history: {replaceState() {}},
    location: {href: 'https://example.invalid/knowledge_governance/#/reviews', hash: '#/reviews',
      replace: url => replacements.push(url)},
    fetch: async (url) => {
      requests.push(url);
      if (reply instanceof Error) throw reply;
      return {status: reply.status, ok: reply.status < 400, json: async () => reply.data};
    },
    recordRender: () => rendered.push(true)
  });
  vm.runInContext(workspaceCode, context);
  // Instrument rendering before the awaited auth request resumes.
  vm.runInContext('render = recordRender;', context);
  await new Promise(resolve => setImmediate(resolve));
  return {replacements, rendered, requests};
}
test('workspace bootstrap fails closed on auth 500', async () => {
  const result = await workspace({status: 500, data: {}});
  assert.deepEqual(result.rendered, []);
  assert.match(result.replacements[0], /login\.html/);
  assert.match(result.replacements[0], /service_unavailable/);
  assert.deepEqual(result.requests, ['api/auth/me']);
});
test('workspace bootstrap fails closed on network failure', async () => {
  const result = await workspace(new Error('synthetic network error'));
  assert.deepEqual(result.rendered, []);
  assert.match(result.replacements[0], /login\.html/);
});
test('workspace bootstrap rejects a malformed success response', async () => {
  const result = await workspace({status: 200, data: {auth_enabled: true}});
  assert.deepEqual(result.rendered, []);
  assert.match(result.replacements[0], /service_unavailable/);
});
test('workspace 401 redirects without losing the business deep link', async () => {
  const result = await workspace({status: 401, data: {}});
  assert.deepEqual(result.rendered, []);
  const target = new URL(result.replacements[0]);
  assert.equal(target.searchParams.get('return_url'), 'https://example.invalid/knowledge_governance/#/reviews');
});
test('explicitly disabled auth can render the development workspace', async () => {
  const result = await workspace({status: 200, data: {auth_enabled: false}});
  assert.equal(result.rendered.length, 1);
  assert.deepEqual(result.replacements, []);
});
