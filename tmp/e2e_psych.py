import asyncio, os, sys, datetime as dt
sys.path.insert(0, '/workspace')
os.environ["ADMIN_PASSWORD"] = "test123"
URL = "postgresql+psycopg://profiler:profiler@localhost:5682/profiling"

from app.config import Settings
from app.models.db import init_db
s = Settings.__new__(Settings); s.database_url = URL
sf = init_db(s)

UID = 9990001112223334
from app.models.entities import Message, UserProfile
from sqlalchemy import select, delete
with sf() as sess:
    if not sess.scalar(select(UserProfile).where(UserProfile.user_id == UID)):
        sess.add(UserProfile(user_id=UID, username="tester", discriminator="0001",
                             bot=False, is_system=False, priority=True,
                             first_seen=dt.datetime.now(dt.timezone.utc),
                             last_updated=dt.datetime.now(dt.timezone.utc)))
    if sess.scalar(select(Message).where(Message.author_id == UID)) is None:
        for i in range(12):
            sess.add(Message(id=UID+i, channel_id=1, guild_id=1, author_id=UID,
                             content=f"тестовое сообщение номер {i} про игры и музыку",
                             timestamp=dt.datetime.now(dt.timezone.utc), clean_content="",
                             type=0))
    sess.commit()
from app.models.entities import Message, UserProfile

# 1. build_prompt_for_user
sys.path.insert(0, ".")
import importlib.util
spec = importlib.util.spec_from_file_location("ep", "export_profiles.py")
ep = importlib.util.module_from_spec(spec); spec.loader.exec_module(ep)
info = ep.build_prompt_for_user(URL, UID)
assert len(info["prompt"]) > 500 and info["messages_total"] >= 12, info
print("PROMPT OK:", info["username"], info["messages_total"], "msgs,", len(info["prompt"]), "chars")

# 2. fake LLM server + llm_client against it
import threading, json as j
from http.server import BaseHTTPRequestHandler, HTTPServer
ANSWER = {"big_five":{"openness":0.7,"conscientiousness":0.5,"extraversion":0.6,"agreeableness":0.4,"neuroticism":0.3},
 "mbti":{"type":"ENTP","alt":["INTP"],"confidence":0.4,"reasoning":"гибкость"},
 "enneagram":{"type":"7","alt":["3"],"confidence":0.3,"reasoning":""},
 "style":"неформальный","tone":{"dominant":"ироничный","triggers":["проигрыш"]},
 "interests":[{"topic":"игры","evidence":"про игры"},{"topic":"музыка","evidence":"про музыку"}],
 "roles":[],"risk_flags":[{"flag":"","evidence":"","severity":"low"}],
 "evidence":[{"signal":"юмор","quote":"тестовое сообщение номер 3"}],
 "data_gaps":[],"confidence_overall":0.5,"disclaimer":"не диагноз"}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); self.rfile.read(n)
        body = j.dumps({"model":"fake/test","choices":[{"message":{"content":"```json\n"+j.dumps(ANSWER, ensure_ascii=False)+"\n```"}}]}).encode()
        self.send_response(200); self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass
srv = HTTPServer(("127.0.0.1", 8899), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["LLM_API_KEY"]="sk-test"; os.environ["LLM_BASE_URL"]="http://127.0.0.1:8899/v1"; os.environ["LLM_MODEL"]="fake/test"
from app.services import llm_client
ans, model_used, ph = llm_client.analyze_prompt(info["prompt"])
assert ans["mbti"]["type"]=="ENTP" and model_used=="fake/test"
print("LLM CLIENT OK:", model_used, ph[:8])

# 3. full route via Flask test client
import admin_app
app = admin_app.create_app(URL)
c = app.test_client()
lr = c.post("/login", data={"pw": "test123"}, follow_redirects=True)
assert "Вход в админку" not in lr.get_data(as_text=True), "login failed"
r = c.post(f"/psych/refresh/{UID}", follow_redirects=True)
html = r.get_data(as_text=True)
assert r.status_code == 200 and ("Психо профиль обновлён" in html or "запись" in html), html[:800]
assert "ENTP" in html, "profile view should show result"
print("ROUTE OK -> profile_view shows ENTP")
# 4. browse user_profiles shows the button
r2 = c.get("/browse/user_profiles"); h2 = r2.get_data(as_text=True)
assert "Обновить психо профиль" in h2 or "Построить психо профиль" in h2
assert "/psych/refresh/" in h2
print("BROWSE BUTTON OK")
# 5. audit log recorded
r3 = c.get("/audit"); assert "psych_refresh" in r3.get_data(as_text=True)
print("AUDIT OK")
srv.shutdown()
