/* BTX → GEM browser workspace.
 * Serve alongside index.html, styles.css and pyscripts.py over HTTP(S).
 * Pyodide runs the supplied Python geometry in a worker. Uploaded files are
 * never sent to a conversion service. Only runtime assets come from CDNs.
 */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.29.3/full/';
  const PLOTLY = 'https://cdn.plot.ly/plotly-2.35.2.min.js';
  const palette = ['#b7e477','#7dcced','#ebae80','#b3a2ed','#82d8bd','#ed9eae','#d9ca81','#85a6e3','#c4b5a5'];
  const state = {ready:false, btx:null, btxName:'', rows:[], model:null, preview:null, exported:null,
    revision:0, modelRevision:-1, floor:'all', options:null, processing:false};
  let worker, workerURL, nextId=0, pending=new Map(), debounce, btxToken=0, csvToken=0, plotlyPromise;
  let renderGeneration=0, bootToken=0;
  const queuedFiles={btx:null,csv:null};
  const fmt = (n, d=1) => Number(n).toLocaleString(undefined,{maximumFractionDigits:d});
  const esc = value => String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const plotText = value => String(value).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  function message(text, success=false) {
    $('message').textContent=text; $('message').classList.toggle('success',success); $('message').hidden=!text;
  }
  function status(text, ready=false) {
    $('model-status').textContent=text; $('model-status').classList.toggle('ready',ready);
  }
  function runtime(text,error=false) {
    $('runtime').textContent=text; $('runtime').classList.toggle('error',error);
  }
  function invoke(payload) {
    const id=++nextId;
    return new Promise((resolve,reject)=>{
      pending.set(id,{resolve,reject}); worker.postMessage({id,payload});
    });
  }
  function rejectPending(text) {for(const task of pending.values()) task.reject(new Error(text)); pending.clear();}
  function loadPlotly() {
    if(window.Plotly) return Promise.resolve(window.Plotly);
    if(plotlyPromise) return plotlyPromise;
    plotlyPromise=new Promise((resolve,reject)=>{
      const script=document.createElement('script'); script.src=PLOTLY; script.async=true;
      const timer=setTimeout(()=>{script.remove(); plotlyPromise=null; reject(new Error('Plotly could not load. Check your connection and retry.'));},90000);
      script.onload=()=>{clearTimeout(timer);resolve(window.Plotly);};
      script.onerror=()=>{clearTimeout(timer);script.remove();plotlyPromise=null;reject(new Error('Plotly could not load. Check your connection and retry.'));};
      document.head.append(script);
    });return plotlyPromise;
  }
  function populateOptions(options) {
    state.options=options;
    for(const [id,key,defaultKey] of [['location','locations','location'],['scale','scales','scale'],['grid','grids','grid']]){
      $(id).replaceChildren();
      for(const [label,value] of Object.entries(options[key])){
        const o=document.createElement('option');o.value=label;
        o.textContent=id==='grid' ? (label==='As drawn"' ? 'As drawn' : label) : label;
        if(id==='grid')o.title=`${value} points`;
        $(id).append(o);
      }
      if(id==='scale')$(id).append(new Option('Custom · 1:N','custom'));
      if(id==='grid')$(id).append(new Option('Custom inches "','custom_inches'),new Option('Custom cm','custom_cm'));
      $(id).value=options.defaults[defaultKey];$(id).disabled=false;
    }
    $('height').disabled=false;updateScaleNote();
  }
  async function boot() {
    const savedConfig=state.options?config():null;
    const token=++bootToken; state.ready=false;
    if(state.options)invalidate();$('retry').hidden=true;runtime('Loading Python…');updateActions();
    if(worker){worker.terminate();rejectPending('Runtime restarted.');}
    if(workerURL)URL.revokeObjectURL(workerURL);
    const workerCode=`
      let py;
      let queue=Promise.resolve();
      self.onmessage=event=>{
        queue=queue.then(async()=>{
          const {id,payload}=event.data;
          try {
            if(payload.action==='init'){
              self.postMessage({status:'Loading Python…'});
              importScripts(${JSON.stringify(PYODIDE+'pyodide.js')});
              py=await loadPyodide({indexURL:${JSON.stringify(PYODIDE)}});
              self.postMessage({status:'Loading NumPy…'});
              await py.loadPackage('numpy');
              await py.runPythonAsync(payload.source);
              py.globals.set('_browser_request',JSON.stringify({action:'options'}));
              self.postMessage({id,result:JSON.parse(py.runPython('browser_dispatch(_browser_request)'))});
              py.globals.delete('_browser_request');
            } else {
              py.globals.set('_browser_request',JSON.stringify(payload));
              try {
                const result=JSON.parse(py.runPython('browser_dispatch(_browser_request)'));
                self.postMessage({id,result});
              } finally {py.globals.delete('_browser_request');}
            }
          } catch(error){self.postMessage({id,error:String(error.message||error)});}
        });
      };
    `;
    try {
      if(location.protocol==='file:')throw new Error('Open this app through a static web server. Keep all four files together, run “python -m http.server 8000” in that folder, then open http://localhost:8000.');
      const response=await fetch(new URL('pyscripts.py',document.baseURI));
      if(!response.ok)throw new Error('pyscripts.py could not be loaded. Keep it beside index.html and serve all four files together.');
      const source=await response.text();
      workerURL=URL.createObjectURL(new Blob([workerCode],{type:'text/javascript'}));worker=new Worker(workerURL);
      worker.onmessage=event=>{
        if(token!==bootToken)return;
        if(event.data.status){runtime(event.data.status);return;}
        const job=pending.get(event.data.id);if(!job)return;pending.delete(event.data.id);
        event.data.error?job.reject(new Error(friendlyError(event.data.error))):job.resolve(event.data.result);
      };
      worker.onerror=event=>{
        state.ready=false;runtime('Python unavailable',true);rejectPending(event.message||'Python worker failed.');
        message('The Python runtime could not start. Check your connection, then retry loading.');$('retry').hidden=false;updateActions();
      };
      const options=await invoke({action:'init',source});if(token!==bootToken)return;
      populateOptions(options);
      if(savedConfig){
        for(const id of ['location','scale','grid'])$(id).value=savedConfig[id];
        $('height').value=savedConfig.height_ft;
        $('custom-scale').value=savedConfig.custom_scale;
        $('custom-grid-inches').value=savedConfig.custom_grid_inches;
        $('custom-grid-cm').value=savedConfig.custom_grid_cm;
        updateScaleNote();
      }
      state.ready=true;runtime('Python ready');
      message('');updateActions();
      for(const kind of ['btx','csv'])if(queuedFiles[kind]){const file=queuedFiles[kind];queuedFiles[kind]=null;await readFile(file,kind);}
      if(state.btx)scheduleAnalysis(0);
      // Plots can load concurrently without holding up file editing.
      loadPlotly().catch(error=>{message(error.message);$('retry').hidden=false;});
    } catch(error) {
      if(token!==bootToken)return;
      runtime('Python unavailable',true);message(error.message);$('retry').hidden=false;
    }
  }
  function friendlyError(raw) {
    const lines=raw.trim().split('\n');
    return (lines.reverse().find(line=>/^(ValueError|ParseError|Error|RuntimeError|UnicodeDecodeError|error|KeyError|TypeError):/.test(line.trim()))||raw).replace(/^[A-Za-z]+Error:\s*/,'').replace(/^error:\s*/,'');
  }
  function config() {return {
    location:$('location').value,scale:$('scale').value,grid:$('grid').value,height_ft:$('height').value,
    custom_scale:$('custom-scale').value,custom_grid_inches:$('custom-grid-inches').value,custom_grid_cm:$('custom-grid-cm').value
  };}
  function payload(action) {return {action,btx:state.btx,rows:state.rows.map(r=>({...r})),config:config()};}
  function updateCustomFields() {
    const scale=$('scale').value==='custom',inches=$('grid').value==='custom_inches',cm=$('grid').value==='custom_cm';
    for(const [wrapper,input,active] of [['custom-scale-field','custom-scale',scale],['custom-inches-field','custom-grid-inches',inches],['custom-cm-field','custom-grid-cm',cm]]){
      $(wrapper).hidden=!active;$(input).disabled=!active;
    }
    $('custom-settings').hidden=!(scale||inches||cm);
    $('custom-grid-help').hidden=!(inches||cm);
  }
  function updateScaleNote() {
    if(!state.options)return;
    updateCustomFields();
    const c=config();
    const grid=c.grid==='custom_inches'?Number(c.custom_grid_inches)*72:c.grid==='custom_cm'?Number(c.custom_grid_cm)/2.54*72:state.options.grids[c.grid];
    const scale=c.scale==='custom'?Number(c.custom_scale):state.options.scales[c.scale];
    if(!Number.isFinite(grid)||grid<=0||!Number.isFinite(scale)||scale<=0){
      $('scale-note').textContent='Enter positive values for the custom grid spacing and page scale.';return;
    }
    const note=c.grid==='As drawn"'?`“As drawn” uses ${state.options.bluebeam_grid_inches} in Bluebeam spacing.`:c.grid.startsWith('custom_')?'Custom spacing is measured on the drawing page.':'';
    $('scale-note').textContent=`Scale 1:${fmt(scale,6)} · Snap grid ${fmt(grid,6)} pt (${fmt(grid*.0254*scale/72,6)} m at this scale)${note?' · '+note:''}`;
  }
  function updateActions() {
    $('review-export').disabled=!state.ready || !state.model?.paired || state.modelRevision!==state.revision || state.processing;
    $('save-csv').disabled=!state.rows.length;
    $('download-gem').disabled=!state.exported;
    const hasPreview=!!state.preview?.length && state.modelRevision===state.revision;
    $('reset-view').disabled=!hasPreview;
    $('viewer-floor').disabled=!hasPreview;
  }
  function clearExport() {
    state.exported=null;
    $('export-note').textContent='Review the live previews, then confirm to create and download your GEM.';
    if($('export-dialog').open)$('export-dialog').close();
  }
  function clearPreview() {
    state.preview=null;
    if(window.Plotly&&$('viewer').data)Plotly.purge($('viewer'));
    $('viewer').hidden=true;$('viewer-empty').hidden=false;
    $('viewer-unassigned').hidden=true;
    $('viewer-empty').querySelector('h3').textContent=state.btx?'Updating 3D model…':'Your live 3D model';
    $('viewer-status').textContent=state.btx?'Updating preview…':'Waiting for BTX';
  }
  function setLivePreview(model) {
    state.preview=model.preview_spaces;
    const selected=$('viewer-floor').value;
    const floors=[...new Set(state.preview.filter(s=>!s.unassigned).map(s=>s.floor))].sort((a,b)=>a-b);
    const unassigned=state.preview.filter(s=>s.unassigned).length;
    $('viewer-floor').replaceChildren(new Option('All floors','all'),...floors.map(f=>new Option(`Floor ${f}`,String(f))));
    if(unassigned)$('viewer-floor').append(new Option('Unassigned','unassigned'));
    if([...$('viewer-floor').options].some(o=>o.value===selected))$('viewer-floor').value=selected;
    $('viewer-unassigned').hidden=!unassigned;
    $('viewer-status').textContent=`Live preview · ${model.paired} paired rooms${unassigned?' · '+unassigned+' unassigned polygons':''}`;
    $('viewer-empty').hidden=true;$('viewer').hidden=false;updateActions();
  }
  function invalidate() {
    clearTimeout(debounce);
    state.revision++;state.modelRevision=-1;++renderGeneration;clearExport();clearPreview();updateActions();
    document.body.classList.toggle('busy',!!state.btx);status(state.btx?'Updating…':'Waiting for BTX');
  }
  function scheduleAnalysis(delay=280) {
    clearTimeout(debounce);debounce=setTimeout(analyze,delay);
  }
  function changed(delay=280) {invalidate();scheduleAnalysis(delay);}
  async function analyze() {
    if(!state.ready||!state.btx)return;
    const revision=state.revision;
    state.processing=true;updateActions();status('Updating…');
    try {
      const model=await invoke(payload('analyze'));
      if(revision!==state.revision)return;
      state.model=model;state.modelRevision=revision;message('');
      renderSummary();renderRowPairing();setLivePreview(model);
      await Promise.all([renderPlans(),renderViewer()]);
      if(revision!==state.revision)return;
      status(model.paired?'Ready for review':'Add room names',!!model.paired);
    }catch(error){
      if(revision!==state.revision)return;
      state.model=null;state.modelRevision=-1;status('Needs attention');message(error.message);
      clearPreview();$('viewer-empty').querySelector('h3').textContent='Check the source files and settings';$('viewer-status').textContent='Preview needs attention';
      clearSummary();clearPlans('Check the source files','Resolve the message above to regenerate the model.');
    }finally{
      if(revision===state.revision){state.processing=false;document.body.classList.remove('busy');updateActions();}
    }
  }
  async function readFile(file,kind) {
    if(!file)return;
    const token=kind==='btx'?++btxToken:++csvToken;
    if(!file.name.toLowerCase().endsWith('.'+kind)){message(`Choose a .${kind} file. The current ${kind.toUpperCase()} is unchanged.`);return;}
    if(file.size>30*1024*1024){message('Please choose a file smaller than 30 MB.');return;}
    if(!state.ready){queuedFiles[kind]=file;$(kind+'-name').textContent=file.name+' · queued';message('Your file is queued and will load when Python is ready.',true);return;}
    // Disable the old export immediately, before file reading or CSV validation.
    invalidate();message('');
    try{
      if(kind==='btx'){
        const bytes=new Uint8Array(await file.arrayBuffer());if(token!==btxToken)return;
        let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
        state.btx=btoa(binary);state.btxName=file.name;
        $('btx-name').textContent=file.name;$('btx-name').title=file.name;$('btx-action').textContent='Replace .btx';
      }else{
        const text=await file.text();if(token!==csvToken)return;
        const result=await invoke({action:'csv',text});if(token!==csvToken)return;
        state.rows=result.rows;
        $('csv-name').textContent=file.name;$('csv-name').title=file.name;$('csv-action').textContent='Replace room names';
        renderTable();
      }
      // A successful replacement gets a fresh revision, so concurrent results
      // cannot overwrite the new file or table.
      changed(0);
      if(!state.btx){document.body.classList.remove('busy');status('Waiting for BTX');}
    }catch(error){
      if(token!==(kind==='btx'?btxToken:csvToken))return;
      state.processing=false;document.body.classList.remove('busy');
      message(`${error.message}\nThe previous ${kind.toUpperCase()} remains loaded. Choose a corrected file to continue.`);status('Upload needs attention');updateActions();
    }finally{$(kind+'-file').value='';}
  }
  function renderTable() {
    const frag=document.createDocumentFragment();
    state.rows.forEach((row,index)=>{
      const tr=document.createElement('tr');tr.dataset.index=index;
      const td=document.createElement('td');td.textContent=index+1;tr.append(td);
      for(const key of ['number','name','floor']){
        const cell=document.createElement('td'),input=document.createElement('input');
        input.value=row[key];input.dataset.key=key;input.dataset.index=index;
        input.setAttribute('aria-label',`Row ${index+1} ${key==='number'?'room number':key}`);
        if(key==='floor'){input.type='number';input.min='1';input.max='1000';input.step='1';}
        else input.type='text';
        cell.append(input);tr.append(cell);
      }
      const cell=document.createElement('td'),button=document.createElement('button');button.className='delete-row';button.type='button';button.textContent='×';button.dataset.remove=index;button.setAttribute('aria-label',`Remove room row ${index+1}`);cell.append(button);tr.append(cell);frag.append(tr);
    });
    $('room-rows').replaceChildren(frag);$('room-empty').hidden=!!state.rows.length;
    $('row-count').textContent=`${state.rows.length} rows`;renderRowPairing();updateActions();
  }
  function renderRowPairing() {
    for(const tr of $('room-rows').children){
      const unpaired=!!state.model&&Number(tr.dataset.index)>=state.model.total_found;
      tr.classList.toggle('unpaired',unpaired);tr.title=unpaired?'No corresponding BTX polygon; excluded from GEM export.':'';
    }
  }
  function clearSummary() {
    for(const id of ['polygons','paired','floors','area'])$('metric-'+id).textContent='—';
    $('floor-summary').textContent='A valid model is needed for a summary.';$('warnings').hidden=true;
  }
  function warningList(el,warnings) {
    el.replaceChildren();el.hidden=!warnings.length;if(!warnings.length)return;
    const ul=document.createElement('ul');warnings.forEach(w=>{const li=document.createElement('li');li.textContent=w;ul.append(li);});el.append(ul);
  }
  function renderSummary() {
    const m=state.model;
    $('metric-polygons').textContent=m.total_found;$('metric-paired').textContent=m.paired;
    $('metric-floors').textContent=m.max_floor||'—';$('metric-area').textContent=m.paired?fmt(m.area_m2):'—';
    const items=Object.entries(m.floor_counts).map(([floor,count])=>`<span><b>Floor ${esc(floor)}</b> · ${count} rooms</span>`);
    if(m.max_floor>m.populated_floors)items.push(`<span>${m.max_floor-m.populated_floors} empty floor levels</span>`);
    items.push(`<span>${fmt(m.height_ft,2)} ft / floor</span>`);
    if(m.paired)items.push(`<span>${fmt(m.area_m2*m.height_m)} m³ room volume</span>`);
    $('floor-summary').innerHTML=items.join('');warningList($('warnings'),m.warnings);updateActions();
  }
  function purgePlans() {if(window.Plotly)for(const el of document.querySelectorAll('.floor-plot'))Plotly.purge(el);}
  function clearPlans(title,description) {++renderGeneration;purgePlans();$('floor-tabs').replaceChildren();$('plans').innerHTML=`<div class="empty-state"><h3>${esc(title)}</h3><p>${esc(description)}</p></div>`;}
  function floorTabs() {
    const m=state.model;const floors=Object.keys(m.floor_counts);
    if(state.floor!=='all'&&!floors.includes(state.floor))state.floor='all';
    $('floor-tabs').replaceChildren();
    const all=[['all','All floors'],...floors.map(f=>[f,`Floor ${f}`])];
    if(!floors.length)return;
    for(const [value,label]of all){const button=document.createElement('button');button.className='tab';button.role='tab';button.id='floor-tab-'+value;button.dataset.floor=value;button.textContent=label;button.setAttribute('aria-selected',String(value===state.floor));button.setAttribute('aria-controls','plans');button.tabIndex=value===state.floor?0:-1;$('floor-tabs').append(button);}
    $('plans').setAttribute('aria-labelledby','floor-tab-'+state.floor);
  }
  async function renderPlans() {
    if(!state.model)return;
    const generation=++renderGeneration;floorTabs();
    try{await loadPlotly();}catch(error){if(generation===renderGeneration)message(error.message);return;}
    if(generation!==renderGeneration)return;
    purgePlans();$('plans').replaceChildren();
    const m=state.model,units=$('units').value,mult=units==='metres'?m.pt_to_m:1,axisUnit=units==='metres'?'m':'pt';
    const groups=[];
    for(const floor of Object.keys(m.floor_counts))if(state.floor==='all'||state.floor===floor)groups.push({title:`Floor ${floor}`,rooms:m.rooms.filter(r=>r.floor===Number(floor))});
    if(state.floor==='all'&&m.total_found>m.paired)groups.push({title:'Unpaired geometry',rooms:m.polygons.slice(m.paired).map((poly,i)=>({index:m.paired+i+1,poly,label:`Polygon ${m.paired+i+1}`})),unpaired:true});
    // Common bounds make plans directly comparable between floors.
    const points=m.polygons.flat(),xs=points.map(p=>p[0]*mult),ys=points.map(p=>p[1]*mult);
    let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
    for(let i=0;i<xs.length;i++){minX=Math.min(minX,xs[i]);maxX=Math.max(maxX,xs[i]);minY=Math.min(minY,ys[i]);maxY=Math.max(maxY,ys[i]);}
    const pad=Math.max(maxX-minX,maxY-minY)*.08||1;
    const pendingPlots = [];
    for(const group of groups){
      if(generation!==renderGeneration)return;
      const card=document.createElement('article');card.className='floor-card';
      const heading=document.createElement('div');heading.className='floor-heading';
      heading.innerHTML=`<h3>${esc(group.title)}</h3><span>${group.rooms.length} ${group.unpaired?'excluded polygons':'rooms'}</span>`;
      const plot=document.createElement('div');plot.className='floor-plot';plot.setAttribute('aria-label',`${group.title} interactive plan`);card.append(heading,plot);$('plans').append(card);
      const traces=[];
      for(const room of group.rooms){
        const ring=room.poly,colour=palette[(room.index-1)%palette.length];
        traces.push({type:'scatter',mode:'lines',x:ring.map(p=>p[0]*mult),y:ring.map(p=>p[1]*mult),fill:'toself',fillcolor:colour+'0b',line:{width:0},hoverinfo:'skip',showlegend:false});
        const normal={x:[],y:[]},diagonal={x:[],y:[]};
        for(let i=0;i<ring.length-1;i++){
          const a=ring[i],b=ring[i+1],target=(a[0]===b[0]||a[1]===b[1])?normal:diagonal;
          target.x.push(a[0]*mult,b[0]*mult,null);target.y.push(a[1]*mult,b[1]*mult,null);
        }
        for(const [coords,c]of [[normal,'#acd8b8'],[diagonal,'#ff8585']])if(coords.x.length)traces.push({type:'scatter',...coords,mode:'lines',line:{color:c,width:1.7},name:plotText(room.label),showlegend:false,hovertemplate:`${plotText(room.label)}<extra></extra>`});
        const open=ring.slice(0,-1),cx=open.reduce((a,p)=>a+p[0],0)/open.length*mult,cy=open.reduce((a,p)=>a+p[1],0)/open.length*mult;
        const mode=$('labels').value;
        if(mode!=='none')traces.push({type:'scatter',x:[cx],y:[cy],mode:'text',text:[plotText(mode==='index'?room.index:room.label)],textfont:{color:'#dfecec',size:11},hoverinfo:'skip',showlegend:false});
      }
      const layout={paper_bgcolor:'#141c20',plot_bgcolor:'#141c20',font:{family:'Segoe UI, Arial',color:'#95aab4',size:10},margin:{l:48,r:15,t:12,b:42},showlegend:false,dragmode:'pan',
        xaxis:{title:{text:`X (${axisUnit})`,standoff:5},range:[minX-pad,maxX+pad],gridcolor:'#29343b',zerolinecolor:'#40505a',tickfont:{size:10},constrain:'domain'},
        yaxis:{title:{text:`Y (${axisUnit})`,standoff:5},range:[minY-pad,maxY+pad],gridcolor:'#29343b',zerolinecolor:'#40505a',scaleanchor:'x',scaleratio:1,constrain:'domain'}};
      pendingPlots.push({plot, traces, layout});
    }
    await new Promise(requestAnimationFrame);
    for (const item of pendingPlots) {
      await Plotly.newPlot(
        item.plot,
        item.traces,
        item.layout,
        {
          responsive: true,
          displaylogo: false,
          scrollZoom: true,
          modeBarButtonsToRemove: [
            'select2d',
            'lasso2d',
            'autoScale2d',
            'toImage'
          ],
          displayModeBar: 'hover'
        }
      );
    }
  }
  function download(text,name,type) {
    const url=URL.createObjectURL(new Blob([text],{type})),a=document.createElement('a');
    a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),15000);
  }
  function saveCSV() {
    const quote=v=>'"'+String(v).replace(/"/g,'""')+'"';
    const text=['Room Number,Name,Floor',...state.rows.map(r=>[r.number,r.name,r.floor].map(quote).join(','))].join('\r\n')+'\r\n';
    download('\ufeff'+text,'Room_Names.csv','text/csv;charset=utf-8');
  }
  function reviewExport() {
    if($('review-export').disabled)return;
    const m=state.model,c=config();
    const gridLabel=c.grid==='custom_inches'?`${c.custom_grid_inches} in (custom)`:c.grid==='custom_cm'?`${c.custom_grid_cm} cm (custom)`:c.grid==='As drawn"'?'As drawn':c.grid;
    const scaleLabel=c.scale==='custom'?`1:${fmt(m.page_scale,6)} (custom)`:c.scale;
    const rows=[['Rooms',`${m.paired} of ${m.total_found} polygons`],['Floors',`${m.populated_floors} populated · highest ${m.max_floor}`],['Floor height',`${fmt(m.height_ft,2)} ft (${fmt(m.height_m,4)} m)`],['Location',c.location],['Page scale',scaleLabel],['Grid density',`${gridLabel} · ${fmt(m.grid_pt,6)} pt`],['Room area',`${fmt(m.area_m2)} m²`]];
    $('confirm-summary').innerHTML=rows.map(([a,b])=>`<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join('');
    warningList($('confirm-warnings'),m.warnings);
    $('accept-mismatch').checked=false;$('mismatch-label').hidden=!m.mismatch;
    $('confirm-export').disabled=m.mismatch;
    $('gem-filename').value=(state.btxName||'model').replace(/\.btx$/i,'');
    $('export-dialog').showModal();
  }
  async function confirmExport() {
    const m=state.model;if(!m||state.modelRevision!==state.revision)return;
    const name=$('gem-filename').value.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/\.gem$/i,'');
    if(!name){$('gem-filename').focus();return;}
    const revision=state.revision;
    $('confirm-export').disabled=true;$('confirm-export').textContent='Creating GEM…';
    try{
      const result=await invoke({...payload('export'),accept_mismatch:$('accept-mismatch').checked});
      if(revision!==state.revision)return;
      state.exported={...result,filename:name+'.gem'};
      download(result.gem,state.exported.filename,'application/octet-stream');$('export-dialog').close();
      $('export-note').textContent=`${state.exported.filename} generated · edit any input to make a new version.`;
      updateActions();
      message('GEM generated. If your browser blocked the download, use “GEM ↓” beside the 3D viewer.',true);
    }catch(error){
      $('export-dialog').close();message(error.message);
    }finally{
      $('confirm-export').textContent='Create & download GEM';$('confirm-export').disabled=false;updateActions();
    }
  }
  async function renderViewer() {
    const preview=state.preview;if(!preview?.length)return;
    try{await loadPlotly();}catch(error){message('The 3D preview could not load. '+error.message);$('retry').hidden=false;return false;}
    if(preview!==state.preview)return;
    const filter=$('viewer-floor').value,traces=[];
    preview.forEach((space,i)=>{
      if(filter==='unassigned'&&!space.unassigned)return;
      if(filter!=='all'&&filter!=='unassigned'&&(space.unassigned||space.floor!==Number(filter)))return;
      const x=[],y=[],z=[];
      // Same per-face, closed polyline packing used by the original parser.
      for(const face of space.faces){for(const index of [...face.indices,face.indices[0]]){const p=space.vertices[index];x.push(p[0]);y.push(p[1]);z.push(p[2]);}x.push(null);y.push(null);z.push(null);}
      traces.push({type:'scatter3d',x,y,z,mode:'lines',name:plotText(space.name),line:{width:3,color:space.unassigned?'#8c9aa3':palette[i%palette.length]},hoverinfo:'name'});
    });
    const axis=title=>({title:{text:title,font:{size:11}},backgroundcolor:'#172025',gridcolor:'#31404a',zerolinecolor:'#52636b',showbackground:true,tickfont:{size:10},color:'#91a8b5'});
    const layout={paper_bgcolor:'#192024',font:{family:'Segoe UI, Arial',color:'#afc0c8',size:11},margin:{l:5,r:5,t:5,b:5},
      scene:{xaxis:axis('X (m)'),yaxis:axis('Y (m)'),zaxis:axis('Z (m)'),aspectmode:'data',camera:{eye:{x:1.45,y:-1.75,z:1.2}}},
      legend:{orientation:'h',x:0,y:0,yanchor:'top',font:{size:11},itemsizing:'constant'}};
    await Plotly.newPlot($('viewer'),traces,layout,{responsive:true,displaylogo:false,scrollZoom:true,modeBarButtonsToRemove:['toImage']});
    return true;
  }
  $('btx-file').addEventListener('change',e=>readFile(e.target.files[0],'btx'));
  $('csv-file').addEventListener('change',e=>readFile(e.target.files[0],'csv'));
  for(const kind of ['btx','csv']){
    const el=$(kind+'-drop');
    el.addEventListener('dragover',e=>{e.preventDefault();el.classList.add('dragover');});
    el.addEventListener('dragleave',()=>el.classList.remove('dragover'));
    el.addEventListener('drop',e=>{e.preventDefault();el.classList.remove('dragover');readFile(e.dataTransfer.files[0],kind);});
  }
  for(const id of ['location','scale','grid'])$(id).addEventListener('change',()=>{updateScaleNote();changed();});
  for(const id of ['custom-scale','custom-grid-inches','custom-grid-cm'])$(id).addEventListener('input',()=>{updateScaleNote();changed();});
  $('height').addEventListener('input',()=>changed());
  $('room-rows').addEventListener('input',event=>{
    const input=event.target;if(!input.dataset.key)return;
    state.rows[Number(input.dataset.index)][input.dataset.key]=input.value;changed();
  });
  $('room-rows').addEventListener('click',event=>{
    const button=event.target.closest('[data-remove]');if(!button)return;
    state.rows.splice(Number(button.dataset.remove),1);renderTable();changed();
  });
  $('add-row').addEventListener('click',()=>{
    state.rows.push({number:String(state.rows.length+1),name:'New room',floor:'1'});renderTable();changed();
    $('room-rows').lastElementChild?.querySelector('[data-key="name"]').focus();
  });
  $('save-csv').addEventListener('click',saveCSV);
  $('floor-tabs').addEventListener('click',event=>{const tab=event.target.closest('[data-floor]');if(!tab)return;state.floor=tab.dataset.floor;renderPlans().catch(e=>message(e.message));});
  $('floor-tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const tabs=[...$('floor-tabs').children],i=tabs.indexOf(document.activeElement);if(i<0)return;
    event.preventDefault();let next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    tabs[next].click();requestAnimationFrame(()=>document.querySelector(`[data-floor="${state.floor}"]`)?.focus());
  });
  for(const id of ['labels','units'])$(id).addEventListener('change',()=>renderPlans().catch(e=>message(e.message)));
  $('review-export').addEventListener('click',reviewExport);
  $('accept-mismatch').addEventListener('change',()=>{$('confirm-export').disabled=state.model?.mismatch&&!$('accept-mismatch').checked;});
  $('confirm-export').addEventListener('click',confirmExport);
  $('viewer-floor').addEventListener('change',()=>renderViewer().catch(e=>message(e.message)));
  $('reset-view').addEventListener('click',()=>renderViewer().catch(e=>message(e.message)));
  $('download-gem').addEventListener('click',()=>{if(state.exported)download(state.exported.gem,state.exported.filename,'application/octet-stream');});
  $('retry').addEventListener('click',boot);
  renderTable();boot();
})();
