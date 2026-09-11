'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let snapshot = {}, data = {}, selected = null, optionSignature = '', confirmation = null, searchRequest = '';
  const ladderState = new Map();
  let pinned = null, explorerSignature = '', streamState = 'connecting', streamLatency = null, latestReceived = 0;
  const explorer = {exchange:'CME', type:'FUT', product:'ES', contract:''};
  const fmt = n => n === null || n === undefined || n === '' ? '—' : typeof n === 'number' ? Number(n.toFixed(8)).toString() : String(n);
  const clock = n => n ? new Date(n).toLocaleTimeString([], {hour12:false}) : '—';
  function quoteStatus(q) {
    if(!snapshot.engine?.alive)return 'Engine offline · historical prices';
    if(!q.timestamp)return q.error||'No quote received from TT';
    const age=Math.max(0,Math.floor((Date.now()-Date.parse(q.timestamp))/1000));
    const precise=Math.max(0,Date.now()-Date.parse(q.timestamp));
    const proof=q.fix_message_type?` · FIX ${q.fix_message_type}${q.fix_sequence?' seq '+q.fix_sequence:''}`:'';
    return q.stale?`Not fresh · last update ${age}s ago${proof}`:`TT FIX UAT · age ${precise} ms${proof}`;
  }
  function applyQuoteFrame(frame) {
    streamState=frame.source==='TT_FIX_UAT'&&frame.simulated===false&&frame.connected?'verified TT FIX stream':'disconnected';
    const received=Object.values(frame.quotes||{}).reduce((latest,q)=>Math.max(latest,Number(q.received_ms)||0),0);
    if(received>latestReceived){latestReceived=received;streamLatency=Math.max(0,Math.round(Date.now()-received));}
    else if(streamLatency===null)streamLatency=Math.max(0,Math.round(Date.now()-Number(frame.published_ms)));
    for(const row of data.watchlist||[]) {
      const quote=frame.quotes?.[row.instrument.security_id];
      if(!quote)continue;
      row.quote={...row.quote,...quote};
      const age=quote.timestamp?Date.now()-Date.parse(quote.timestamp):Infinity;
      row.quote.stale=quote.integrity_ok===false||!frame.connected||age>15000;
      row.quote.spread=quote.ask!==null&&quote.ask!==undefined&&quote.bid!==null&&quote.bid!==undefined?quote.ask-quote.bid:null;
      row.quote.mid=quote.ask!==null&&quote.ask!==undefined&&quote.bid!==null&&quote.bid!==undefined?(quote.ask+quote.bid)/2:null;
    }
    const md=snapshot.engine?.fix_connection?.sessions?.find(s=>s.name==='Market Data');
    $('feed-status').textContent=`Source: TT ${snapshot.engine?.environment||'UAT'} FIX · ${streamState} · FIX-to-screen ${streamLatency} ms · Last heartbeat: ${clock(md?.last_heartbeat)}. Quote age is measured in milliseconds from the last TT update.`;
    renderWatch();renderLadders();
  }
  function connectQuoteStream() {
    if(!window.EventSource)return;
    const source=new EventSource('/api/quotes/stream');
    source.onopen=()=>streamState='streaming';
    source.onmessage=event=>{try{applyQuoteFrame(JSON.parse(event.data));}catch(error){streamState='invalid frame';}};
    source.onerror=()=>{streamState='reconnecting';};
  }
  const text = (tag, value, cls) => {const e = document.createElement(tag); e.textContent = value; if (cls) e.className = cls; return e;};
  function notice(message, error=false) { $('notice').textContent = message; $('notice').className = 'feedback' + (error ? ' error' : ''); }
  async function request(url, options) {
    const r = await fetch(url, {cache:'no-store', ...options});
    if (!r.ok) throw new Error(`Request failed (${r.status}). Check the FIX engine.`);
    return r.json();
  }
  async function command(operation, args) {
    const queued = await request('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'terminal_'+operation, args})});
    if (!queued.ok) throw new Error(queued.error || 'Request refused');
    const deadline = Date.now()+45000;
    while (Date.now()<deadline) {
      const r = await request('/api/result/'+encodeURIComponent(queued.id));
      if (!r.pending) {
        if (!r.ok) throw new Error(r.error || 'Request failed');
        return r;
      }
      await new Promise(resolve=>setTimeout(resolve,150));
    }
    throw new Error('Engine reply timed out. Check status before retrying an order.');
  }
  function button(label, handler, cls='') {
    const b = text('button', label, cls); b.type='button';
    b.addEventListener('click', async()=>{b.disabled=true;try{await handler();}catch(e){notice(e.message,true);}finally{b.disabled=false;}});
    return b;
  }
  function empty(body, count, message) {const row=document.createElement('tr'), cell=text('td',message);cell.colSpan=count;row.append(cell);body.append(row);}
  function showInstrument(instrument) {
    selected=instrument;
    $('instrument-description').textContent=instrument.description || instrument.symbol;
    const dl=document.createElement('dl');
    for (const [name,key] of [['TT Security ID','security_id'],['Product symbol','symbol'],['Exchange','exchange'],['Type','security_type'],['Expiry month','maturity'],['Expiry date','expiry_date'],['Currency','currency'],['Tick size (FIX units)','tick_size'],['Tick value','tick_value'],['Point value','point_value'],['Display factor (metadata)','display_factor'],['Contract code','contract_code'],['Contract multiplier','multiplier'],['Minimum trade volume','min_qty'],['Strike','strike'],['Put / call (0 put, 1 call)','put_call'],['Strategy subtype','subtype']]) {
      const wrap=document.createElement('div');wrap.append(text('dt',name),text('dd',fmt(instrument[key])));dl.append(wrap);
    }
    $('instrument-details').replaceChildren(dl);
    $('legs').textContent=instrument.legs?.length ? instrument.legs.map((l,i)=>`${i+1}. ${l['600']||l['602']||'Leg'} · ${l['610']||''} · ${l['624']==='1'?'Buy':l['624']==='2'?'Sell':l['624']||'Side unspecified'} · ratio ${l['623']||'—'} · ID ${l['602']||'—'}`).join('\n') : 'No leg definition returned for this instrument.';
    const rows=document.createDocumentFragment();
    for (const [tag,value] of Object.entries(instrument.parameters||{})) {const row=document.createElement('tr');row.append(text('td',tag),text('td',value));rows.append(row);}
    $('parameters').replaceChildren(rows);
  }
  function ticketFor(instrument, side) {
    $('ticket-instrument').value=instrument.security_id;
    $('ticket-form').elements.side.value=side;
    showInstrument(instrument);
    const quote=data.watchlist?.find(w=>w.instrument.security_id===instrument.security_id)?.quote;
    if (quote && !quote.stale) {
      const value=side==='BUY'?quote.ask:quote.bid;
      if (value!==null && value!==undefined) $('ticket-form').elements.price.value=value;
    }
    $('ticket-form').scrollIntoView({behavior:'smooth',block:'center'});
  }
  function renderResults() {
    if (searchRequest && data.search?.request_id !== searchRequest) {
      $('results').replaceChildren(); empty($('results'),8,'Waiting for the new TT search…');
      $('search-status').textContent='Searching…'; return;
    }
    const rows=document.createDocumentFragment(), filter=$('filter').value.toLowerCase();
    const month=new Date().toISOString().slice(0,7).replace('-','');
    const found=(data.instruments||[]).filter(i=>[i.description,i.display_name,i.symbol,i.security_id,i.maturity].join(' ').toLowerCase().includes(filter)).sort((a,b)=>{
      const am=a.maturity||'',bm=b.maturity||'';
      return Number(am<month)-Number(bm<month)||am.localeCompare(bm);
    });
    for (const instrument of found.slice(0,200)) {
      const row=document.createElement('tr'), first=document.createElement('td');first.className='wrap';
      first.append(button(instrument.display_name||instrument.description||instrument.symbol,()=>showInstrument(instrument)));row.append(first);
      for (const key of ['symbol','exchange','security_type','maturity','security_id','currency']) row.append(text('td',fmt(instrument[key])));
      const action=document.createElement('td');
      action.append(button('Add + subscribe',async()=>{await command('add',{security_id:instrument.security_id});showInstrument(instrument);notice('Symbol saved. Waiting for TT market data.');}));row.append(action);rows.append(row);
    }
    $('results').replaceChildren(rows);
    if (!found.length) empty($('results'),8,'No matching instruments received. Check exchange/product and the search status.');
    const search=data.search||{};
    $('search-status').textContent=`${search.status||'Idle'} · ${(data.instruments||[]).length} received${found.length>200?' · showing first 200; use filter':''}${search.error?' · '+search.error:''}`;
  }
  function renderWatch() {
    const rows=document.createDocumentFragment();
    for (const {instrument:i,quote:q} of data.watchlist||[]) {
      const row=document.createElement('tr'), name=document.createElement('td');name.className='wrap';
      name.append(button(i.display_name||i.description||i.symbol,()=>showInstrument(i)),text('div',i.security_id+' / '+i.maturity,'tiny'));row.append(name);
      for (const key of ['bid_size','bid','ask','ask_size','last','mid','spread']) row.append(text('td',fmt(q[key]),q.stale?'stale':key==='bid'?'bid':key==='ask'?'ask':''));
      const stamp=text('td',clock(q.timestamp));
      stamp.append(text('div',quoteStatus(q),q.stale?'stale':'quote-live'));
      if (q.error) stamp.append(text('div',q.error,'quote-message'));
      row.append(stamp);
      const actions=document.createElement('td');actions.append(button('Buy',()=>ticketFor(i,'BUY'),'buy'),button('Sell',()=>ticketFor(i,'SELL'),'sell'),button('Ladder',()=>{if(!pinned)pinned=[];if(!pinned.includes(i.security_id)){if(pinned.length>=4)throw new Error('Close a ladder before opening another.');pinned.push(i.security_id);}renderLadders();$('ladders').scrollIntoView({behavior:'smooth'});}),button('Remove',async()=>{await command('remove',{security_id:i.security_id});notice('Removed from the watchlist.');}));row.append(actions);rows.append(row);
    }
    $('watchlist').replaceChildren(rows);
    if (!data.watchlist?.length) empty($('watchlist'),10,'Add a returned TT instrument to subscribe to its prices.');
    const signature=(data.watchlist||[]).map(w=>w.instrument.security_id).join(',');
    if(signature!==optionSignature){
      const previous=$('ticket-instrument').value;optionSignature=signature;
      $('ticket-instrument').replaceChildren(new Option('Select an instrument',''));
      for(const {instrument:i} of data.watchlist||[]) $('ticket-instrument').append(new Option(`${i.display_name||i.description||i.symbol} · ${i.maturity||i.security_type} · ${i.security_id}`,i.security_id));
      if ([...$('ticket-instrument').options].some(o=>o.value===previous)) $('ticket-instrument').value=previous;
    }
  }
  function confirmDialog(title, content, label, callback) {
    $('review-title').textContent=title;$('review-content').replaceChildren(content);$('confirm').textContent=label;
    $('review-error').textContent='';confirmation=callback;$('confirm').disabled=false;$('review-dialog').showModal();
  }
  async function reviewClose(order) {
    const review=await command('preview_close',{order_id:order.id}), ticket=review.ticket;
    const content=document.createElement('div');
    content.append(text('h3',ticket.instrument.display_name||ticket.instrument.symbol),
      text('p',`${ticket.side} ${ticket.quantity} at MARKET · Account ${ticket.account} · Open/Close: CLOSE`),
      text('p',`Closes fills from ${order.id}. Market execution price is not guaranteed.`),
      text('p','Quantity is based on this ticket’s fills. Confirm it is still open in TT if you traded it elsewhere.'));
    confirmDialog('Review close position',content,'Confirm · Close position',()=>command('submit',{token:review.token,confirmed:true}));
  }
  function renderLadders() {
    const watched=data.watchlist||[];
    if(pinned===null && watched.length)pinned=[...watched].sort((a,b)=>Number(!a.quote.timestamp)-Number(!b.quote.timestamp)||(a.instrument.maturity||'').localeCompare(b.instrument.maturity||'')).slice(0,4).map(w=>w.instrument.security_id);
    const root=document.createDocumentFragment();
    for(const id of pinned||[]) {
      const item=watched.find(w=>w.instrument.security_id===id);if(!item)continue;
      const i=item.instrument,q=item.quote;
      if(!ladderState.has(id))ladderState.set(id,{qty:'1',tif:'DAY',type:'LIMIT',locked:false,center:null,price:null});
      const state=ladderState.get(id), card=document.createElement('section');card.className='ladder';
      const heading=document.createElement('div');heading.className='ladder-title';
      heading.append(text('strong',i.display_name||i.symbol),button('×',()=>{pinned=pinned.filter(k=>k!==id);renderLadders();}));card.append(heading);
      card.append(text('div',`${i.exchange} · ${i.security_type} · FIX prices`,'muted'));
      card.append(text('div',`Bid ${fmt(q.bid)}   Ask ${fmt(q.ask)}   Spread ${fmt(q.spread)}`,'ladder-quote'));
      card.append(text('div',quoteStatus(q),q.stale?'stale':'quote-live'));
      const controls=document.createElement('div');controls.className='ladder-controls';
      const qty=document.createElement('input');qty.type='number';qty.min='0.00000001';qty.step='any';qty.value=state.qty;qty.setAttribute('aria-label','Order quantity '+(i.display_name||id));qty.oninput=()=>state.qty=qty.value;
      const type=document.createElement('select');for(const t of ['LIMIT','MARKET'])type.append(new Option(t,t));type.value=state.type;type.onchange=()=>state.type=type.value;
      const tif=document.createElement('select');for(const t of ['DAY','GTC','IOC','FOK'])tif.append(new Option(t,t));tif.value=state.tif;tif.onchange=()=>state.tif=tif.value;
      controls.append(qty,type,tif);card.append(controls);
      const actions=document.createElement('div');actions.className='ladder-actions';
      for(const side of ['BUY','SELL'])actions.append(button('Review '+side,()=>{
        const current=data.watchlist?.find(w=>w.instrument.security_id===id)?.quote;
        if(!current||current.stale||!snapshot.engine?.alive)throw new Error('Wait for fresh quotes before using the ladder.');
        ticketFor(i,side);const form=$('ticket-form');form.elements.quantity.value=state.qty;form.elements.order_type.value=state.type;form.elements.tif.value=state.tif;
        if(state.price!==null)form.elements.price.value=state.price;
        conditionals();form.requestSubmit();
      },side==='BUY'?'buy':'sell'));
      actions.append(button('Centre',()=>{state.center=null;state.price=null;renderLadders();}));
      const lock=document.createElement('label');lock.className='check';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=state.locked;checkbox.onchange=()=>state.locked=checkbox.checked;lock.append(checkbox,text('span','Lock centre'));actions.append(lock);card.append(actions);
      const levels=document.createElement('table');levels.className='ladder-levels';const head=document.createElement('tr');for(const h of ['Working','Bid qty','Price','Ask qty'])head.append(text('th',h));levels.append(head);
      const tick=Number(i.tick_size), midpoint=q.mid??q.bid??q.ask;
      if(tick>0&&midpoint!==null&&midpoint!==undefined) {
        if(state.center===null||!state.locked)state.center=Math.round(midpoint/tick)*tick;
        const bids=q.bid_levels?.length?q.bid_levels:[{price:q.bid,size:q.bid_size}], asks=q.ask_levels?.length?q.ask_levels:[{price:q.ask,size:q.ask_size}];
        for(let offset=15;offset>=-15;offset--){
          const price=Number((state.center+offset*tick).toPrecision(14)),row=document.createElement('tr');
          if(state.price===price)row.className='selected-price';
          const equal=value=>value!==null&&value!==undefined&&Math.abs(Number(value)-price)<tick/100;
          const work=(data.orders||[]).filter(o=>o.ticket.security_id===id&&!['FILLED','CANCELED','REJECTED','EXPIRED','UNKNOWN'].includes(o.status)&&equal(o.ticket.price));
          row.append(text('td',work.map(o=>`${o.ticket.side[0]} ${fmt(o.remaining_qty)}`).join(', ')));
          const bid=bids.find(l=>equal(l.price)),ask=asks.find(l=>equal(l.price));
          row.append(text('td',bid?fmt(bid.size):'','ladder-bid'),text('td',fmt(price),'ladder-price'),text('td',ask?fmt(ask.size):'','ladder-ask'));
          row.onclick=()=>{state.price=price;state.locked=true;renderLadders();};levels.append(row);
        }
      }else{const row=document.createElement('tr'),cell=text('td','Waiting for a quote and a valid tick size');cell.colSpan=4;row.append(cell);levels.append(row);}
      const scroll=document.createElement('div');scroll.className='ladder-scroll';scroll.append(levels);card.append(scroll);
      const depth=button(i.full_depth?'Use top of book':'Request full depth',async()=>{await command('depth',{security_id:id,enabled:!i.full_depth});notice('Market-data subscription updated.');});card.append(depth);
      card.append(button('Refresh quotes',async()=>{await command('depth',{security_id:id,enabled:!!i.full_depth});notice('Requested a new TT snapshot. Waiting for venue data.');}));
      for(const order of (data.orders||[]).filter(o=>o.ticket.security_id===id&&o.close_available>0))card.append(button(`Close ${fmt(order.close_available)} ${order.ticket.side} fills`,()=>reviewClose(order)));
      card.append(text('div',i.full_depth?'Venue depth; unreported quantities stay blank.':'Top-of-book feed; other quantities are blank.','muted'));root.append(card);
    }
    // Avoid replacing an input while the operator types quantity or chooses a TIF.
    if(!$('ladders').contains(document.activeElement)||!['INPUT','SELECT'].includes(document.activeElement.tagName))$('ladders').replaceChildren(root);
  }
  function renderExplorer(force=false) {
    const catalogue=data.catalogue||data.instruments||[];
    const signature=catalogue.map(i=>i.security_id).join(',')+JSON.stringify(explorer);
    if(!force&&signature===explorerSignature)return;explorerSignature=signature;
    const options=(id, values, selected)=>{$(id).replaceChildren();for(const [value,label] of values)$(id).append(new Option(label,value));$(id).value=selected;};
    options('explore-exchange',[...new Set([explorer.exchange,...catalogue.map(i=>i.exchange)])].sort().map(v=>[v,v]),explorer.exchange);
    $('explore-exchange-input').value=explorer.exchange;
    options('explore-type',[['FUT','Future'],['MLEG','Spread / Strategy'],['OPT','Option'],['CS','Stock'],['FOR','Forward'],['SPOT','Spot']],explorer.type);
    const matching=catalogue.filter(i=>i.exchange===explorer.exchange&&i.security_type===explorer.type);
    options('explore-product',[...new Set(matching.map(i=>i.symbol))].sort().map(v=>[v,v]),explorer.product);
    $('explore-product-input').value=explorer.product;
    const contracts=matching.filter(i=>i.symbol===explorer.product).sort((a,b)=>(a.maturity||'').localeCompare(b.maturity||''));
    if(!contracts.some(i=>i.security_id===explorer.contract))explorer.contract='';
    options('explore-contract',contracts.map(i=>[i.security_id,`${i.display_name||i.symbol} ${i.maturity||''}`]),explorer.contract);
    $('explore-status').textContent=`${contracts.length} cached contracts. Search contracts to request current definitions from TT.`;
  }
  function renderOrders() {
    const rows=document.createDocumentFragment();
    for (const order of data.orders||[]) {
      const t=order.ticket, row=document.createElement('tr');row.append(text('td',clock(order.updated)));
      const name=text('td',t.instrument.display_name||t.instrument.description||t.instrument.symbol,'wrap');name.append(text('div',order.id,'tiny'));row.append(name);
      row.append(text('td',`${t.side} ${t.order_type}`),text('td',`${t.quantity} / ${fmt(order.filled_qty)}`),text('td',`${fmt(t.price)} / ${fmt(t.stop_price)}`),text('td',order.status+(order.pending?' · '+order.pending.kind+' requested':'')));
      const reason=text('td',order.venue_order_id||'Awaiting ID','wrap');reason.append(text('div',order.text||'','quote-message'));row.append(reason);
      const actions=document.createElement('td');
      if (order.close_available > 0) {
        actions.append(button('Close position', async()=>{
          const review=await command('preview_close',{order_id:order.id}), ticket=review.ticket;
          const content=document.createElement('div');
          content.append(text('h3',ticket.instrument.display_name||ticket.instrument.symbol),
            text('p',`${ticket.side} ${ticket.quantity} at MARKET · Account ${ticket.account} · Open/Close: CLOSE`),
            text('p',`Closes fills from ${order.id}. Market execution price is not guaranteed.`),
            text('p','This quantity is based on this ticket’s fills. Confirm the position is still open in TT if you traded it elsewhere.'));
          confirmDialog('Review close position',content,'Confirm · Close position',()=>command('submit',{token:review.token,confirmed:true}));
        }));
      }
      if (order.closed_qty > 0) actions.append(text('div',`Closed: ${fmt(order.closed_qty)}`,'tiny'));
      if (order.close_of) actions.append(text('div','Closing '+order.close_of,'tiny'));
      if (['NEW','PARTIALLY_FILLED','REPLACED'].includes(order.status)&&!order.pending) {
        actions.append(button('Cancel',()=>confirmDialog('Cancel UAT order',text('p',`Request cancellation of ${order.id}? It remains working until TT confirms cancellation.`),'Request cancel',()=>command('cancel',{order_id:order.id}))));
        if (!order.close_of) actions.append(button('Replace',()=>{
          const form=document.createElement('div');form.className='form-grid ticket-fields';
          for(const [key,label] of [['quantity','New total quantity'],['price','New limit price'],['stop_price','New stop trigger']]) {
            const l=text('label',label),input=document.createElement('input');input.type='number';input.step='any';input.name=key;input.value=t[key]??'';l.append(input);form.append(l);
          }
          confirmDialog('Review order replacement',form,'Send replacement',()=>{const args={order_id:order.id};form.querySelectorAll('input').forEach(i=>args[i.name]=i.value);return command('replace',args);});
        }));
      }
      row.append(actions);rows.append(row);
    }
    $('orders').replaceChildren(rows);if(!data.orders?.length)empty($('orders'),8,'No orders sent from this manual ticket.');
    const fills=document.createDocumentFragment();for(const f of data.fills||[]){const row=document.createElement('tr');for(const value of [clock(f.time),f.symbol,f.side,f.quantity,f.price,f.exec_id])row.append(text('td',fmt(value)));fills.append(row);}
    $('fills').replaceChildren(fills);if(!data.fills?.length)empty($('fills'),6,'No executions received.');
  }
  function conditionals() {
    const type=$('order-type').value, tif=$('tif').value, form=$('ticket-form');
    const limit=['LIMIT','STOP_LIMIT','LIMIT_ON_CLOSE','POST_ONLY'].includes(type), stop=['STOP','STOP_LIMIT'].includes(type);
    for(const [id,name,needed] of [['price-label','price',limit],['stop-label','stop_price',stop],['expiry-label','expire_date',tif==='GTD']]){
      $(id).hidden=!needed;form.elements[name].disabled=!needed;form.elements[name].required=needed;
    }
  }
  $('search-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;searchRequest='pending';$('filter').value='';renderResults();try{const result=await command('search',Object.fromEntries(new FormData(e.target)));searchRequest=result.request_id;notice('Search sent to TT. Results arrive asynchronously.');}catch(error){searchRequest='';notice(error.message,true);}finally{b.disabled=false;}});
  $('ticket-form').addEventListener('submit',async e=>{
    e.preventDefault();$('review-order').disabled=true;
    try{
      const args=Object.fromEntries(new FormData(e.target));args.cancel_on_disconnect=e.target.elements.cancel_on_disconnect.checked;
      const r=await command('preview',args), wrap=document.createElement('div'), dl=document.createElement('dl');
      for(const [key,value] of Object.entries(r.ticket)){if(key==='instrument')continue;const div=document.createElement('div');div.append(text('dt',key.replaceAll('_',' ')),text('dd',fmt(value)));dl.append(div);}
      wrap.append(text('h3',r.ticket.instrument.display_name||r.ticket.instrument.description||r.ticket.instrument.symbol),dl,text('p','Review expires in 60 seconds. Confirming sends this order to TT UAT. Exchange acceptance is reported below.'));
      const details=document.createElement('details');details.append(text('summary','FIX fields to be sent'),text('pre',r.fields.map(([k,v])=>`${k}=${v}`).join('\n')));wrap.append(details);
      confirmDialog('Review UAT order',wrap,'Confirm · Send UAT order',()=>command('submit',{token:r.token,confirmed:true}));
    }catch(error){notice(error.message,true);}finally{$('review-order').disabled=false;}
  });
  $('confirm').addEventListener('click',async()=>{if(!confirmation)return;$('confirm').disabled=true;try{const r=await confirmation();confirmation=null;$('review-dialog').close();notice(`Request sent${r.order_id?' · '+r.order_id:''}. Wait for TT acknowledgement.`);}catch(e){$('review-error').textContent=e.message;}finally{$('confirm').disabled=false;}});
  $('abort').addEventListener('click',()=>{$('review-dialog').close();confirmation=null;});
  $('filter').addEventListener('input',renderResults);
  $('order-type').addEventListener('change',conditionals);$('tif').addEventListener('change',conditionals);
  $('ticket-instrument').addEventListener('change',()=>{const i=data.watchlist?.find(w=>w.instrument.security_id===$('ticket-instrument').value)?.instrument;if(i)showInstrument(i);});
  async function poll(){
    try{
      const streamed=new Map((data.watchlist||[]).map(row=>[row.instrument.security_id,row.quote]));
      snapshot=await request('/api/snapshot');const engine=snapshot.engine||{};data=engine.manual_terminal||{};
      for(const row of data.watchlist||[]) {
        const newer=streamed.get(row.instrument.security_id);
        if(newer?.timestamp&&(!row.quote.timestamp||Date.parse(newer.timestamp)>Date.parse(row.quote.timestamp)))row.quote=newer;
      }
      const md=engine.fix_connection?.sessions?.find(s=>s.name==='Market Data');
      $('feed-status').textContent=`Source: TT ${engine.environment||'UAT'} FIX · Market data: ${md?.status||'DISCONNECTED'} · quote stream: ${streamState}${streamLatency===null?'':` · FIX-to-screen ${streamLatency} ms`} · Last heartbeat: ${clock(md?.last_heartbeat)}. UAT is a test feed; quote age reflects actual TT updates.`;
      $('session').textContent=!engine.alive?'Engine offline':`${engine.environment||''} · ${engine.session?.state||'DOWN'}`;
      $('session').className='tag '+(engine.alive&&engine.session?.state==='LOGGED_ON'?'good':'error');
      $('review-order').disabled=!engine.alive||engine.session?.state!=='LOGGED_ON';
      if(!engine.alive)notice('The engine is offline. Prices below are historical and orders are unavailable.',true);
      if(!engine.alive)for(const w of data.watchlist||[])w.quote.stale=true;
      if(!$('account').value&&data.account)$('account').value=data.account;
      renderResults();renderWatch();renderOrders();renderLadders();renderExplorer();
    }catch(error){for(const w of data.watchlist||[])w.quote.stale=true;if(snapshot.engine)snapshot.engine.alive=false;$('session').textContent='Engine unavailable';$('session').className='tag error';$('review-order').disabled=true;notice(error.message,true);renderWatch();renderLadders();}
    setTimeout(poll,1000);
  }
  $('explore-open').onclick=()=>{renderExplorer(true);$('explore-dialog').showModal();};
  $('explore-cancel').onclick=()=>$('explore-dialog').close();
  for(const [id,key] of [['explore-exchange','exchange'],['explore-type','type'],['explore-product','product'],['explore-contract','contract']])$(id).onchange=()=>{explorer[key]=$(id).value;if(key!=='contract')explorer.contract='';renderExplorer(true);};
  $('explore-product-input').oninput=()=>{explorer.product=$('explore-product-input').value.trim();explorer.contract='';$('explore-contract').value='';};
  $('explore-exchange-input').oninput=()=>{explorer.exchange=$('explore-exchange-input').value.trim();explorer.contract='';$('explore-contract').value='';};
  $('explore-search').onclick=async()=>{try{searchRequest='pending';const r=await command('search',{exchange:explorer.exchange,security_type:explorer.type,symbol:explorer.product});searchRequest=r.request_id;notice('Instrument explorer search sent.');}catch(e){searchRequest='';notice(e.message,true);$('explore-status').textContent=e.message;}};
  $('explore-add').onclick=async()=>{try{if(!explorer.contract)throw new Error('Select an instrument first');await command('add',{security_id:explorer.contract});$('explore-dialog').close();notice('Instrument added and subscribed.');}catch(e){$('explore-status').textContent=e.message;}};
  conditionals();connectQuoteStream();poll();
})();
