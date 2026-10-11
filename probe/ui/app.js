const $ = id => document.getElementById(id);
let current = {};
function render(state) {
  current = state; $('state').textContent = state.state + (state.paused ? ' · 已暂停采集' : '');
  $('pair').hidden = state.configured; $('connected').hidden = !state.configured; $('login').hidden = !state.configured;
  $('server-name').textContent = state.server; $('pause').textContent = state.paused ? '恢复采集' : '暂停采集';
  $('confirm-login').disabled = !state.login_active;
  const previous = $('profile').value; $('profile').replaceChildren();
  for (const p of state.profiles) { const option = document.createElement('option'); option.value = p.id; option.textContent = p.name; $('profile').appendChild(option); }
  if (state.profiles.some(p => p.id === previous)) $('profile').value = previous;
  $('notice').textContent = state.notice || (state.operation ? '正在执行浏览器采集，请保持Chrome在线' : '');
}
async function action(button, fn) {
  button.disabled = true;
  try { const result = await fn(); if (result && !result.ok) $('notice').textContent = result.message; }
  finally { button.disabled = false; }
}
$('register').onclick = () => action($('register'), () => window.probe.register({ server: $('server').value.trim(), config: $('config').value.trim(), name: $('name').value.trim(), consent: $('consent').checked }));
$('config').oninput = () => {
  try {
    const text = $('config').value.trim();
    if (text.startsWith('hrprobe1.') && text.length <= 40000) {
      const data = JSON.parse(atob(text.slice(9).replace(/-/g, '+').replace(/_/g, '/')));
      if (typeof data.server === 'string' && data.server.startsWith('https://')) $('server').value = data.server;
    }
  } catch { /* Main process validates the full pairing configuration. */ }
};
$('pause').onclick = () => window.probe.pause(!current.paused);
$('reconnect').onclick = () => window.probe.reconnect();
$('startup').onchange = () => window.probe.startup($('startup').checked);
$('unpair').onclick = () => { if (confirm('解除这台探针与平台的配对？浏览器资料仍保留。')) action($('unpair'), () => window.probe.unpair()); };
$('open-login').onclick = () => action($('open-login'), () => window.probe.openLogin({ kind: $('kind').value, name: $('account-name').value.trim(), profile_id: $('profile').value }));
$('confirm-login').onclick = () => action($('confirm-login'), () => window.probe.confirmLogin());
window.probe.onState(render); window.probe.status().then(render);
