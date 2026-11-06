import './quantum-panel.js';
let token = '';
const panel = document.querySelector('quantum-provider-panel');
async function api(path, body) {
  const response = await fetch('api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json().catch(() => ({error: 'HTTP ' + response.status}));
  if (!response.ok) throw Error(result.error || 'Request failed');
  return result;
}
async function state() {
  const catalog = await api('providers'); const snapshots = {};
  for (const provider of catalog.providers) {
    try { snapshots[provider.id] = await api('providers/' + encodeURIComponent(provider.id) + '/snapshot'); }
    catch (error) { snapshots[provider.id] = {error: error.message}; }
  }
  return {providers: catalog.providers, definitions: catalog.definitions, snapshots};
}
panel.transport = async (action, data, provider) => {
  if (['state', 'refresh'].includes(action)) return state();
  if (action === 'configure') return api('providers', data);
  return api('providers/' + encodeURIComponent(provider) + '/' + action, action === 'inspect' ? undefined : data);
};
async function connect() {
  panel.state = await state();
  document.querySelector('#auth').hidden = true; document.querySelector('#content').hidden = false;
}
document.querySelector('#login').onsubmit = async event => {
  event.preventDefault(); document.querySelector('#error').textContent = '';
  token = document.querySelector('#token').value; document.querySelector('#token').value = '';
  try { await connect(); } catch (error) { document.querySelector('#error').textContent = error.message; }
};
document.querySelector('#copy-token').onclick = async () => {
  try {
    if (navigator.clipboard) await navigator.clipboard.writeText(token);
    else { const input = document.createElement('textarea'); input.value = token; document.body.append(input); input.select(); document.execCommand('copy'); input.remove(); }
  } catch { document.querySelector('#error').textContent = 'Could not copy the app token'; }
};
try { const response = await fetch('bootstrap'); if (response.ok) { token = (await response.json()).token; await connect(); } }
catch (error) { document.querySelector('#error').textContent = error.message; }
