'use strict';
const loginButton = document.querySelector('#login-button');
const retryButton = document.querySelector('#retry-button');
const statusBox = document.querySelector('#login-status');
const params = new URLSearchParams(location.search);
const baseUrl = new URL('./', location.href);
const messages = {
  state_invalid: '登录授权校验失败，请重新发起登录。',
  state_expired: '登录授权已过期，请重新发起登录。',
  code_missing: '授权未完成或已取消，请重新登录。',
  corp_mismatch: '请使用本企业的钉钉账号登录。',
  authorization_failed: '钉钉授权失败，请重试；持续失败请联系管理员检查应用配置。',
  authorization_unavailable: '暂时无法连接钉钉授权服务，请稍后重试。',
  session_store_unavailable: '登录会话服务暂时不可用，请联系管理员检查数据库连接。',
  service_unavailable: '登录服务暂时不可用，请重新检查或稍后重试。'
};
function returnTarget() {
  const fallback = new URL('./', location.href);
  fallback.hash = '#/overview';
  try {
    const target = new URL(params.get('return_url') || './', baseUrl);
    if (target.origin !== baseUrl.origin || ![baseUrl.pathname, baseUrl.pathname + 'index.html'].includes(target.pathname)) return fallback.href;
    // A server cannot read a fragment. Browsers preserve it across the entry
    // redirect; carry it back into the signed return URL to restore deep links.
    if (!target.hash) target.hash = location.hash || '#/overview';
    return target.href;
  } catch (_) { return fallback.href; }
}
function status(message, error = false) {
  statusBox.textContent = message;
  statusBox.classList.toggle('error', error);
}
async function authRequest(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(new URL(path, baseUrl), {signal: controller.signal, cache: 'no-store'});
    const data = await response.json();
    return {response, data};
  } finally { clearTimeout(timer); }
}
async function checkSession() {
  loginButton.disabled = true;
  loginButton.textContent = '正在检查登录状态…';
  retryButton.hidden = true;
  status('正在连接登录服务，请稍候。');
  try {
    const {response, data} = await authRequest('api/auth/me');
    if (response.ok && (data.auth_enabled === false || data.user?.union_id)) {
      status('正在进入工作台…');
      location.replace(returnTarget());
      return;
    }
    if (response.status !== 401) throw new Error('service_unavailable');
    loginButton.disabled = false;
    loginButton.textContent = '使用钉钉账号登录';
    const errorMessage = messages[params.get('error')];
    status(errorMessage || '点击后前往钉钉授权，可扫码或使用已登录的账号。', !!errorMessage);
  } catch (_) {
    loginButton.textContent = '登录服务暂时不可用';
    retryButton.hidden = false;
    status(messages.service_unavailable, true);
  }
}
loginButton.addEventListener('click', async () => {
  loginButton.disabled = true;
  loginButton.textContent = '正在跳转钉钉授权…';
  retryButton.hidden = true;
  try {
    const {response, data} = await authRequest('api/auth/login-url?return_url=' + encodeURIComponent(returnTarget()));
    if (!response.ok || !data.login_url) throw new Error(data.detail?.message || messages.authorization_failed);
    const target = new URL(data.login_url);
    if (target.protocol !== 'https:' || target.hostname !== 'login.dingtalk.com') throw new Error(messages.authorization_failed);
    location.assign(target.href);
  } catch (error) {
    loginButton.disabled = false;
    loginButton.textContent = '使用钉钉账号登录';
    status(error.name === 'AbortError' ? '登录服务响应超时，请重试。' : error.message, true);
  }
});
retryButton.addEventListener('click', checkSession);
checkSession();
