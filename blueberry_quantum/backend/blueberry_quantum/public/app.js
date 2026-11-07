import './quantum-panel.js';
const panel = document.querySelector('quantum-provider-panel');
async function api(path, body) {
  const response = await fetch('api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
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
  document.querySelector('#error').textContent = '';
  document.querySelector('#retry').hidden = true;
  try { panel.state = await state(); }
  catch (error) { document.querySelector('#error').textContent = error.message; document.querySelector('#retry').hidden = false; }
}
document.querySelector('#retry').onclick = connect;
await connect();
