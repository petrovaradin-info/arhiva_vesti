"""Portable review page and transactional import of explicitly selected decisions."""
import hashlib
import json
from pathlib import Path

from .relevance import RelevanceReview, fingerprint


def review_packet(review, batch_id):
    row = review.db.execute('SELECT payload FROM relevance_batches WHERE id=?', (batch_id,)).fetchone()
    if not row:
        raise ValueError('Nepoznat paket.')
    batch = json.loads(row[0])
    articles = []
    for article in batch['articles']:
        current = review.show(article['id'])
        if fingerprint(current['text'] or '') != article['text_hash']:
            raise ValueError(f"Tekst #{article['id']} je promenjen. Napravi novi paket.")
        predictions = [json.loads(p['payload']) for p in current['predictions']
                       if p['batch_id'] == batch_id]
        articles.append(dict(article, status=current['status'],
            revision=current['decisions'][-1]['id'] if current['decisions'] else 0,
            history=current['decisions'], prediction=predictions[-1] if predictions else None))
    return {'batch_id': batch_id, 'rules_version': batch['rules_version'], 'articles': articles}


def export_page(review, batch_id, out):
    packet = review_packet(review, batch_id)
    # Escape script delimiters even though article values are rendered with textContent.
    data = json.dumps(packet, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HTML.replace('__PACKET__', data), encoding='utf-8')
    return len(packet['articles'])


def import_decisions(review, payload, apply=False):
    if not isinstance(payload, dict) or payload.get('format') != 'petrovaradin-human-review-v1':
        raise ValueError('Nepoznat format odluka.')
    items = payload.get('decisions')
    if not isinstance(items, list) or not items:
        raise ValueError('Nema izabranih odluka.')
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    review.db.execute('''CREATE TABLE IF NOT EXISTS relevance_imports (
      digest TEXT PRIMARY KEY, imported_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    review.db.commit()
    with review.db:
        review.db.execute('BEGIN IMMEDIATE')
        if review.db.execute('SELECT 1 FROM relevance_imports WHERE digest=?', (digest,)).fetchone():
            return {'count': 0, 'already_imported': True, 'applied': False}
        packet = review_packet(review, payload.get('batch_id'))
        articles = {a['id']: a for a in packet['articles']}
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Neispravna odluka.')
            row_id = item.get('id')
            if type(row_id) is not int or row_id not in articles or row_id in seen:
                raise ValueError('Nepoznat ili ponovljen ID.')
            seen.add(row_id)
            article = articles[row_id]
            if any(item.get(key) != article[key] for key in ('text_hash', 'revision', 'status')):
                raise ValueError(f'Vest #{row_id} je izmenjena posle otvaranja pregleda. Osveži pregled.')
            if article['status'] not in {'awaiting_review', 'archived', 'rejected'}:
                raise ValueError('Vest nije spremna za pregled.')
            review.validate(item.get('label'), item.get('reason'), item.get('evidence'), article['text'])
        if apply:
            for item in items:
                review._decide(item['id'], item['label'], item['reason'], item['evidence'])
            review.db.execute('INSERT INTO relevance_imports(digest) VALUES (?)', (digest,))
    return {'count': len(items), 'already_imported': False, 'applied': apply}


HTML = r'''<!doctype html><html lang="sr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Petrovaradin · pregled vesti</title>
<style>
:root{font-family:system-ui,sans-serif;color:#18332d;background:#f3f5f0}body{max-width:1040px;margin:0 auto;padding:28px 20px 100px}h1{font-size:32px;margin-bottom:8px}p{line-height:1.6}.muted{color:#52655f}header{margin-bottom:24px}article{background:white;border:1px solid #dbe3dc;border-radius:14px;padding:24px;margin:18px 0}h2{font-size:21px;line-height:1.4}a{color:#17664f}blockquote{border-left:3px solid #6e9b86;padding:12px 16px;margin:12px 0;background:#f2f7f3;white-space:pre-wrap}.badge{display:inline-block;background:#e5eee7;padding:5px 10px;border-radius:20px;font-size:13px}.warn{background:#fff0d4;color:#694600}label{display:block;margin-top:16px;font-weight:600}textarea,select,input{font:inherit;box-sizing:border-box}textarea{width:100%;min-height:74px;padding:10px;border:1px solid #acbfb1;border-radius:7px}select{padding:10px;background:white;border:1px solid #acbfb1;border-radius:7px}button{padding:11px 18px;border:0;border-radius:7px;background:#145943;color:white;font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:default}details{margin-top:16px}.full{white-space:pre-wrap;line-height:1.65;max-height:420px;overflow:auto;padding:12px;background:#f7f8f5}.bar{position:sticky;top:0;z-index:2;background:#f3f5f0f5;padding:12px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #cad7cc}#notice{color:#704900}footer{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #cad7cc;padding:16px;display:flex;justify-content:center;gap:20px;align-items:center}.check{display:flex;gap:10px;align-items:center}.check input{width:20px;height:20px}small{display:block;margin-top:6px}summary{cursor:pointer} @media(max-width:600px){body{padding:15px 12px 140px}article{padding:16px}footer{flex-wrap:wrap}h1{font-size:26px}}
</style>
<header><div class="badge">PETROVARADIN · ARHIVA</div><h1>Tvoja provera vesti</h1>
<p>Pročitaj predlog i dokaz, izaberi svoju odluku i označi „Potvrđujem ovu odluku“. Možeš pregledati samo deo paketa. Nijedna odluka nije unapred potvrđena.</p>
<p class="muted">Izvoz preuzima fajl sa tvojim odlukama. Baza se menja tek kada taj fajl uvezemo. Relevantnost ne potvrđuje istinitost navoda u vesti.</p></header>
<div class="bar"><input id="search" placeholder="Pretraži naslov ili ID" aria-label="Pretraga"><select id="filter" aria-label="Filter"><option value="all">Sve vesti</option><option value="uncertain">Predlog: proveriti</option><option value="pending">Još nepotvrđene</option><option value="selected">Izabrane za izvoz</option></select><span id="counter"></span></div>
<p id="notice" role="status" aria-live="polite"></p><main id="list"></main>
<footer><span id="count"></span><button id="export">Preuzmi potvrđene odluke</button></footer>
<script id="packet" type="application/json">__PACKET__</script><script>
const packet=JSON.parse(document.getElementById('packet').textContent);
const names={direct:'Zadrži — direktna veza',context:'Zadrži — istorijski ili posredni kontekst',irrelevant:'Odbaci — nije relevantno',uncertain:'Nisam siguran — dodatna provera'};
const key='petrovaradin-review-v1:'+packet.batch_id;let drafts={};
try{drafts=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){document.getElementById('notice').textContent='Lokalno čuvanje nije dostupno; izvezi odluke pre zatvaranja.'}
const cards=[];const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
for(const a of packet.articles){
 const p=a.prediction;let d=drafts[a.id];
 if(!d||d.revision!==a.revision||d.text_hash!==a.text_hash||d.status!==a.status)d={label:'',reason:p?.reason||'',evidence:p?.evidence||'',selected:false,revision:a.revision,text_hash:a.text_hash,status:a.status};
 drafts[a.id]=d;const card=el('article');card.dataset.id=a.id;
 const badge=el('span','#'+a.id+' · '+(p?names[p.label]:'Bez predloga'));badge.className='badge'+(p?.label==='uncertain'?' warn':'');card.append(badge,el('h2',a.title||'Bez naslova'));
 const link=el('a','Otvori izvorni članak ↗');try{const u=new URL(a.url);if(['https:','http:'].includes(u.protocol))link.href=u.href}catch(e){}link.target='_blank';link.rel='noopener noreferrer';card.append(link);
 card.append(el('p',p?'Razlog predloga: '+p.reason:'Predlog modela još nije unet.'));
 if(p)card.append(el('blockquote',p.evidence));
 const detail=el('details');detail.append(el('summary','Prikaži ceo sačuvani tekst'));const full=el('div',a.text);full.className='full';detail.append(full);card.append(detail);
 if(a.history.length){const h=el('details');h.append(el('summary','Prethodne odluke ('+a.history.length+')'));for(const x of a.history)h.append(el('p',names[x.label]+' — '+x.reason));card.append(h)}
 const sel=el('select');sel.setAttribute('aria-label','Tvoja odluka za '+a.id);sel.append(new Option('Izaberi svoju odluku',''));for(const [v,t]of Object.entries(names))sel.append(new Option(t,v));sel.value=d.label;
 const lab=el('label','Tvoja odluka');lab.append(sel);card.append(lab);
 const reason=el('textarea');reason.value=d.reason;const rl=el('label','Razlog / tvoja napomena');rl.append(reason);card.append(rl);
 const evidence=el('textarea');evidence.value=d.evidence;const ev=el('label','Dokaz — doslovan odlomak iz sačuvanog teksta');ev.append(evidence);card.append(ev);
 const check=el('input');check.type='checkbox';check.checked=d.selected;const cl=el('label');cl.className='check';cl.append(check,el('span','Potvrđujem ovu odluku i uključujem je u izvoz'));card.append(cl);
 function changed(){d.label=sel.value;d.reason=reason.value;d.evidence=evidence.value;d.selected=check.checked;try{localStorage.setItem(key,JSON.stringify(drafts))}catch(e){document.getElementById('notice').textContent='Nacrt nije sačuvan u pregledaču. Izvezi pre zatvaranja.'}refresh()}
 for(const x of [sel,reason,evidence,check])x.addEventListener('change',changed);
 document.getElementById('list').append(card);cards.push({a,card,d});
}
function refresh(){const q=document.getElementById('search').value.toLocaleLowerCase();const f=document.getElementById('filter').value;let shown=0;for(const {a,card,d}of cards){const match=(a.id+' '+a.title).toLocaleLowerCase().includes(q)&&(f==='all'||f==='uncertain'&&a.prediction?.label==='uncertain'||f==='pending'&&!d.selected||f==='selected'&&d.selected);card.hidden=!match;if(match)shown++}const count=cards.filter(x=>x.d.selected).length;document.getElementById('count').textContent=count+' / '+cards.length+' potvrđeno za izvoz';document.getElementById('counter').textContent='Prikazano: '+shown;document.getElementById('export').disabled=!count}
document.getElementById('search').addEventListener('input',refresh);document.getElementById('filter').addEventListener('change',refresh);
document.getElementById('export').onclick=()=>{const decisions=[];for(const {a,d}of cards){if(!d.selected)continue;if(!names[d.label]||!d.reason.trim()||!d.evidence.trim()||!a.text.includes(d.evidence)){document.getElementById('notice').textContent='Proveri odluku, razlog i doslovan dokaz za #'+a.id;return}decisions.push({id:a.id,label:d.label,reason:d.reason,evidence:d.evidence,text_hash:a.text_hash,revision:a.revision,status:a.status})}const data={format:'petrovaradin-human-review-v1',batch_id:packet.batch_id,decisions};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=el('a');a.href=url;a.download='odluke-'+packet.batch_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('notice').textContent='Fajl sa '+decisions.length+' odluka je preuzet. Pošalji ga ovde ili uvezi komandom iz uputstva. Baza još nije promenjena.'};refresh();
</script></html>'''
