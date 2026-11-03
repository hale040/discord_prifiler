import sys; import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.profile_export.transcript import make_transcript
from app.services.profile_export.stats import ping_targets

assert ping_targets("hey <@1252199565675724821> and <@!909467040417460284>, look <#123> @everyone") == [1252199565675724821, 909467040417460284]
assert ping_targets("") == []

T = 1252199565675724821
O2 = 909467040417460284
msgs = [
 dict(id=1, author_id=O2, content="пост про релиз", clean_content="пост про релиз", timestamp="2026-11-01T10:00:00+00:00", reference_id=None, reply_to_author_id=None, edited_at=None, deleted_at=None, has_attachment=False, embed_count=0, reactions_json=[]),
 dict(id=2, author_id=T, content=f"слушай <@{O2}> а когда дедлайн?", clean_content="слушай @а когда дедлайн?", timestamp="2026-11-01T10:05:00+00:00", reference_id=None, reply_to_author_id=None, edited_at=None, deleted_at=None, has_attachment=False, embed_count=0, reactions_json=[]),
 dict(id=3, author_id=T, content=f"спасибо <@{O2}>", clean_content="спасибо", timestamp="2026-11-01T10:06:00+00:00", reference_id=1, reply_to_author_id=O2, edited_at=None, deleted_at=None, has_attachment=False, embed_count=0, reactions_json=[]),
]
names = {T: "Целевой", O2: "Вася"}
txt, trunc = make_transcript(msgs, None, False, target_uid=T, names=names)
print(txt)
lines = txt.split("\n")
ping_line = next(l for l in lines if "дедлайн" in l)
reply_line = next(l for l in lines if "спасибо" in l)
assert "pO1" in ping_line, "нет флага пинга: " + ping_line
assert ">1" in reply_line and "pO1" not in reply_line, "reply не должен давать p: " + reply_line
assert "pUk" in lines[0], "легенда не объясняет pUk"
print("PING_TESTS=PASS")
