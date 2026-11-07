/* Shared schema-driven UI. Provider names, settings, actions and operations come from the API. */
function element(tag, text, attributes = {}) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}
function schemaForm(schema, values = {}, submitLabel = 'Save', onSubmit, disabled = false) {
  const form = element(submitLabel ? 'form' : 'div');
  const readers = [];
  for (const [key, property] of Object.entries(schema.properties || {})) {
    const label = element('label', property.title || key);
    let control;
    const value = values[key] ?? property.default;
    if (property.type === 'object') {
      const nested = schemaForm(property, value || {}, '', null, disabled);
      control = element('fieldset'); control.append(nested);
      readers.push(() => [key, nested.read()]);
    } else if (property.enum) {
      control = element('select');
      for (let index = 0; index < property.enum.length; index++) control.add(new Option(property.enumNames?.[index] || String(property.enum[index]), String(index)));
      const selected = property.enum.findIndex(item => JSON.stringify(item) === JSON.stringify(value));
      if (selected >= 0) control.value = String(selected);
      readers.push(() => [key, property.enum[Number(control.value)]]);
    } else if (property.type === 'boolean') {
      control = element('input', undefined, {type: 'checkbox'}); control.checked = !!value;
      readers.push(() => [key, control.checked]);
    } else {
      const array = property.type === 'array';
      control = element(array || property.type === 'object' ? 'textarea' : 'input');
      if (!array) control.type = property.writeOnly ? 'password' : ['integer', 'number'].includes(property.type) ? 'number' : 'text';
      control.autocomplete = property.writeOnly ? 'off' : 'on';
      control.value = property.writeOnly ? '' : array ? (value || []).map(item => typeof item === 'object' ? JSON.stringify(item) : String(item)).join('\n') : value ?? '';
      if (property.minimum !== undefined) control.min = property.minimum;
      if (property.maximum !== undefined) control.max = property.maximum;
      if (property.type === 'integer') control.step = '1';
      readers.push(() => {
        if (!control.value.trim() && !(schema.required || []).includes(key)) return [key, undefined];
        if (array) return [key, control.value.split(['object', 'array'].includes(property.items?.type) ? /[\n]/ : /[\n,]/).map(item => item.trim()).filter(Boolean).map(item => ['object', 'array'].includes(property.items?.type) ? JSON.parse(item) : property.items?.type === 'integer' ? Number(item) : item)];
        return [key, ['number', 'integer'].includes(property.type) ? Number(control.value) : control.value];
      });
    }
    control.name = key; control.dataset.field = key;
    control.disabled = disabled || property.readOnly || (property['x-immutable'] && values[key] !== undefined) || property.const !== undefined;
    if (property.const !== undefined) {
      if (control.type === 'checkbox') control.checked = property.const;
      else control.value = String(property.const);
    }
    if (!property.writeOnly && (schema.required || []).includes(key)) control.required = true;
    if (property['x-hidden']) label.hidden = true;
    label.append(control);
    if (property.description) label.append(element('small', property.description));
    form.append(label);
  }
  form.read = () => Object.fromEntries(readers.map(read => read()).filter(([key, value]) => value !== undefined && !schema.properties[key].readOnly));
  if (submitLabel) {
    const button = element('button', submitLabel, {type: 'submit'});
    button.disabled = disabled || Object.values(schema.properties || {}).some(property => property.enum?.length === 0);
    form.append(button);
  }
  form.onsubmit = event => { event.preventDefault(); if (onSubmit) onSubmit(form.read(), form); };
  return form;
}
class QuantumProviderPanel extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}); this.data = {providers: [], snapshots: {}, definitions: {}}; }
  connectedCallback() { this.render(); }
  set state(value) { this.data = value || this.data; this.render(); }
  async perform(work) {
    if (this.busy) return;
    this.busy = true; this.message = ''; this.failed = false;
    const main = this.shadowRoot.querySelector('main'); if (main) main.inert = true;
    try { await work(); } catch (error) { this.message = error.message; this.failed = true; this.render(); }
    finally { this.busy = false; const main = this.shadowRoot.querySelector('main'); if (main) main.inert = false; }
  }
  async request(action, data = {}, provider = this.selected) {
    if (!this.transport) throw Error('Management connection is not ready');
    return this.transport(action, data, provider);
  }
  async reload() { this.data = await this.request('state'); this.render(); }
  async review(resource, operation, values) {
    const provider = this.selected;
    const plan = await this.request('plan', {resource: resource.id, operation, values}, provider);
    if (provider !== this.selected) throw Error('Provider changed. Review the change again');
    this.plan = plan; this.render();
  }
  renderProviderEditor(container) {
    const details = element('details'); details.append(element('summary', 'Provider settings'));
    const types = element('select', undefined, {'aria-label': 'Provider type'});
    const definitions = this.data.definitions || {};
    for (const [kind, definition] of Object.entries(definitions)) types.add(new Option(definition.title, kind));
    const selected = this.data.providers.find(row => row.id === this.selected);
    let editing = !!selected;
    if (selected && definitions[selected.kind]) types.value = selected.kind;
    const editor = element('div');
    const mode = element('p'); mode.className = 'muted';
    const add = element('button', 'Add another provider', {type:'button'});
    const edit = element('button', 'Edit selected provider', {type:'button'}); edit.disabled = !selected;
    const render = () => {
      editor.replaceChildren();
      const definition = definitions[types.value]; if (!definition) return;
      mode.textContent = editing ? 'Editing ' + (selected.name || definition.title) : 'Creating a new provider';
      types.disabled = editing;
      const current = editing ? selected : {};
      const form = schemaForm(definition.configuration, current, editing ? 'Save provider settings' : 'Create provider', (values, form) => this.perform(async () => {
        const body = {...values, kind: types.value};
        if (editing) body.id = selected.id;
        const result = await this.request('configure', body);
        form.reset(); this.selected = result.id; this.plan = null;
        this.message = 'Provider settings saved.';
        await this.reload();
      }));
      editor.append(form);
    };
    add.onclick = () => { editing = false; render(); };
    edit.onclick = () => { editing = true; types.value = selected.kind; render(); };
    types.onchange = render;
    details.append(mode, add, edit, types, editor); render(); container.append(details);
  }
  renderActions(container, snapshot) {
    if (!snapshot.actions?.length) return;
    const details = element('details'); details.append(element('summary', 'Provider actions and sign-in'));
    for (const action of snapshot.actions) {
      details.append(element('h4', action.title));
      details.append(schemaForm(action.schema, {}, action.title, (values, form) => this.perform(async () => {
        form.reset(); const result = await this.request(action.transport || 'invoke', action.transport ? values : {action: action.id, values});
        this.plan = null; this.message = result.message || 'Action completed'; await this.reload();
      })));
    }
    container.append(details);
  }
  renderResource(container, resource) {
    const section = element('details'); section.dataset.resource = resource.id; section.open = !!resource.read;
    section.append(element('summary', resource.title));
    if (resource.reason) section.append(element('p', resource.reason));
    if (resource.notice) section.append(element('small', resource.notice));
    if (!resource.read) { container.append(section); return; }
    const operations = resource.operations || {};
    const editor = element('div');
    const editRecord = record => {
      editor.replaceChildren(); if (resource.collection) editor.append(element('h4', 'Edit ' + resource.title.toLowerCase()));
      editor.append(schemaForm(resource.schema, record || {}, operations.upsert || 'Save settings', values => this.perform(() => this.review(resource, 'upsert', values)), !resource.write));
    };
    if (resource.collection) {
      const table = element('table'); const header = table.createTHead().insertRow();
      for (const key of resource.columns || []) header.append(element('th', resource.schema.properties?.[key]?.title || key));
      header.append(element('th', 'Actions'));
      for (const row of resource.value || []) {
        const tr = table.insertRow();
        for (const key of resource.columns || []) tr.insertCell().textContent = typeof row[key] === 'boolean' ? row[key] ? resource.schema.properties?.[key]?.trueLabel || 'On' : resource.schema.properties?.[key]?.falseLabel || 'Off' : row[key] ?? '';
        const cell = tr.insertCell();
        for (const [operation, title] of Object.entries(operations)) {
          const button = element('button', operation === 'upsert' ? 'Edit' : typeof title === 'string' ? title : title.title);
          button.dataset.operation = operation; button.disabled = !resource.write;
          button.onclick = () => this.perform(async () => {
            if (operation === 'upsert') { editRecord(row); return; }
            const schema = resource.operation_schemas?.[operation];
            if (schema && Object.keys(schema.properties || {}).some(key => key !== 'id')) {
              editor.replaceChildren(schemaForm(schema, row, typeof title === 'string' ? title : title.title, values => this.perform(() => this.review(resource, operation, values))));
            } else await this.review(resource, operation, {id: String(row.id)});
          });
          cell.append(button);
        }
      }
      section.append(table);
      if (!resource.value?.length) section.append(element('p', 'No records'));
      if (resource.allow_create && operations.upsert) {
        const add = element('button', 'Add ' + resource.title.toLowerCase()); add.disabled = !resource.write;
        add.onclick = () => editRecord(); section.append(add);
      }
    } else if (operations.upsert) editRecord(resource.value);
    else section.append(element('pre', JSON.stringify(resource.value, null, 2)));
    section.append(editor); container.append(section);
  }
  renderPlan(container) {
    if (!this.plan || this.plan.provider !== this.selected) return;
    const section = element('section'); section.dataset.review = '';
    const provider = this.data.providers.find(row => row.id === this.selected);
    const resource = this.data.snapshots[this.selected]?.resources?.find(row => row.id === this.plan.resource);
    section.append(element('h3', 'Review change for ' + (provider?.name || provider?.kind || this.selected)));
    section.append(element('p', (resource?.title || this.plan.resource) + ' / ' + (resource?.operations?.[this.plan.operation] || this.plan.operation)));
    const values = element('dl');
    for (const [key, value] of Object.entries(this.plan.values)) {
      values.append(element('dt', resource?.schema?.properties?.[key]?.title || key), element('dd', Array.isArray(value) ? value.join(', ') : typeof value === 'boolean' ? value ? 'On' : 'Off' : String(value)));
    }
    section.append(values);
    if (this.plan.notice) section.append(element('p', this.plan.notice));
    const apply = element('button', 'Apply reviewed change'); apply.dataset.apply = '';
    apply.onclick = () => this.perform(async () => {
      const plan = this.plan;
      if (!plan || plan.provider !== this.selected) throw Error('Review a change for the selected provider');
      this.plan = null; apply.disabled = true;
      await this.request('apply', {plan_id: plan.id}); this.message = 'Change submitted. Current state refreshed.'; await this.reload();
    });
    section.append(apply); container.append(section);
  }
  render() {
    const root = this.shadowRoot; root.replaceChildren();
    root.append(element('style', `
      :host{display:block;font-family:var(--paper-font-body1_-_font-family,inherit);color:var(--primary-text-color,inherit)}
      main{padding:20px}[hidden]{display:none!important}h2{font-size:20px;line-height:1.4;margin:0 0 14px}h3,h4{font-size:14px;margin:16px 0 10px}
      label{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:10px 0;font-size:13px}
      input,select,textarea,button{font:inherit;font-size:13px;color:var(--primary-text-color,inherit);background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:9px;padding:9px 11px;max-width:100%;min-width:0;box-sizing:border-box}
      input:not([type=checkbox]),select,textarea{flex:1;min-width:160px}textarea{min-height:64px}
      input[type=checkbox]{accent-color:var(--primary-color,#4595d1);width:17px;height:17px}
      button{cursor:pointer;margin:4px}button:hover{background:var(--secondary-background-color,#f5f5f5)}button:disabled{opacity:.5;cursor:default}
      button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--primary-color,#4595d1);outline-offset:2px}
      details{border-top:1px solid var(--divider-color,#ddd);margin-top:14px;padding:12px 0}summary{cursor:pointer;font-size:14px;font-weight:500}
      table{width:100%;text-align:left;border-collapse:collapse;display:block;overflow:auto;font-size:13px}th,td{padding:8px;border-bottom:1px solid var(--divider-color,#ddd)}td button{font-size:12px;padding:6px 8px}
      small,.muted{display:block;font-size:12px;line-height:1.6;color:var(--secondary-text-color,#667)}label small{flex-basis:100%}
      section{border:1px solid var(--primary-color,#4595d1);border-radius:9px;padding:12px;margin-top:14px}
      .error{color:var(--error-color,#c33)}[role=alert]{font-size:13px;line-height:1.5}dd{margin-bottom:8px;overflow-wrap:anywhere}
      @media(max-width:400px){main{padding:14px}input:not([type=checkbox]),select,textarea{width:100%;flex-basis:100%}}
    `));
    const main = element('main'); main.append(element('h2', this.title || 'BlueberryQuantum'));
    const message = element('p', this.message || '', {role: 'alert'}); if (this.failed) message.className = 'error'; main.append(message);
    const providers = this.data.providers || [];
    if (!providers.some(row => row.id === this.selected)) this.selected = providers[0]?.id;
    const selector = element('select', undefined, {'aria-label': 'Provider'});
    for (const row of providers) selector.add(new Option(row.name || this.data.definitions?.[row.kind]?.title || row.kind, row.id));
    selector.value = this.selected || '';
    selector.onchange = () => { this.selected = selector.value; this.plan = null; this.message = ''; this.render(); };
    main.append(selector);
    for (const [action, title] of [['refresh', 'Refresh'], ['inspect', 'Inspect provider'], ['reconnect', 'Reconnect'], ['delete', 'Remove provider']]) {
      const button = element('button', title); button.disabled = !this.selected;
      button.onclick = () => this.perform(async () => {
        if (action === 'refresh') { this.data = await this.request('refresh'); this.render(); return; }
        if (action === 'delete' && !confirm('Remove the selected provider configuration?')) return;
        const result = await this.request(action);
        this.plan = null;
        if (action === 'inspect') this.message = result.message || 'Provider inspection completed';
        await this.reload();
      }); main.append(button);
    }
    this.renderProviderEditor(main);
    const snapshot = this.data.snapshots?.[this.selected] || {};
    if (snapshot.error) main.append(element('p', snapshot.error));
    this.renderActions(main, snapshot);
    this.renderPlan(main);
    for (const resource of snapshot.resources || []) this.renderResource(main, resource);
    main.inert = !!this.busy; root.append(main);
  }
}
if (!customElements.get('quantum-provider-panel')) customElements.define('quantum-provider-panel', QuantumProviderPanel);
export {QuantumProviderPanel, schemaForm};
