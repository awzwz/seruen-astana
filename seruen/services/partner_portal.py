"""Partner portal: a venue proposes its opening hours. Edits wait for moderation and are labelled as
"данные заведения" — never mixed silently with OSM data, never promoted for money."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from seruen.common.clients import endpoint
from seruen.common.config import settings
from seruen.common.stores import pg_enabled, pg_pool, publish

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seruen — кабинет заведения</title><style>
body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#11222B;background:#F2F5F6}
h1{font-size:24px}label{display:block;margin:14px 0 4px;font-weight:600}input,textarea{width:100%;padding:10px;border:1px solid #C5D1D7;border-radius:8px;font:inherit}
button{margin-top:16px;padding:12px 18px;border:0;border-radius:10px;background:#00849B;color:#fff;font:600 16px system-ui}
.note{background:#FBF0D3;border-left:3px solid #B07A00;padding:10px 14px;border-radius:0 8px 8px 0;margin-top:18px;font-size:14px}
#ok{margin-top:14px;font-weight:600}</style></head><body>
<h1>Кабинет заведения</h1><p>Предложите актуальные часы работы. После проверки модератором они появятся в Seruen с пометкой «данные заведения».</p>
<form id="f"><label for="poi">ID места в Seruen</label><input id="poi" required placeholder="osm:way/123456">
<label for="hours">Часы работы (формат OpenStreetMap)</label><input id="hours" required placeholder="Mo-Fr 09:00-21:00; Sa-Su 10:00-22:00">
<label for="contact">Контакт для проверки</label><input id="contact" placeholder="телефон или сайт заведения">
<button type="submit">Отправить на проверку</button><div id="ok" role="status"></div></form>
<p class="note">Платного продвижения нет: правка влияет только на точность часов, а не на место в выдаче.</p>
<script>document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();
const r=await fetch('/partner/v1/hours',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({poi_id:poi.value,raw:hours.value,contact:contact.value})});
const j=await r.json();document.getElementById('ok').textContent=j.ok?'Спасибо! Правка отправлена на модерацию.':'Не получилось: '+(j.error||'ошибка');});</script>
</body></html>"""


@endpoint("partner-portal", "/v1/partner/hours")
async def hours(p: dict) -> dict:
    pid, raw = (p.get("poi_id") or "").strip(), (p.get("raw") or "").strip()
    if not pid or not raw or len(raw) > 200:
        return {"ok": False, "error": "bad_request"}
    try:
        from opening_hours import OpeningHours

        OpeningHours(raw)
    except Exception:
        return {"ok": False, "error": "hours_syntax"}
    rec = {"poi_id": pid, "raw": raw, "contact": (p.get("contact") or "")[:120], "status": "pending",
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if pg_enabled():
        with pg_pool().connection() as conn:
            conn.execute("insert into partner_edit(poi_id, raw, contact, status) values (%s,%s,%s,'pending')",
                         (pid, raw, rec["contact"]))
    else:
        publish("partner.edits", pid, rec)
    return {"ok": True, "status": "pending"}


def extra(app: FastAPI) -> None:
    @app.get("/partner", response_class=HTMLResponse)
    async def page():
        return PAGE

    @app.post("/partner/v1/hours")
    async def submit(body: dict):
        return await hours(body)
