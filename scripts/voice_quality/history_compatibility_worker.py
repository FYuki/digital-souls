
import hashlib,json,sqlite3,sys
from datetime import UTC,datetime,timedelta
from pathlib import Path
from uuid import UUID
import app.conversation_history.repository as module
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import initialize_conversation_history_schema,SCHEMA_VERSION
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.conversation_history.models import ProcessingTurnInput,PrivacySkippedTurnInput
from app.privacy.contracts import HistoryDecisionReasonCode
db=Path(sys.argv[1]);phase=sys.argv[2]
assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[3]).resolve()/"backend")
clock=lambda:datetime(2026,9,15,tzinfo=UTC)
def identity(n):return UUID(f"10000000-0000-4000-8000-{n:012d}")
sequence=iter({"before":[1,2,3,4,5],"after":[6],"rollback":[7],"verify":[]}[phase])
initialize_conversation_history_schema(db)
repo=ConversationHistoryRepository(database_path=db,stale_after=timedelta(hours=1),retention=timedelta(days=30),clock=clock,uuid_factory=lambda:identity(next(sequence)),wal_cleanup=ConversationWalCleanup(database_path=db,clock=clock,connection_factory=sqlite3.connect))
def snapshot():
 with sqlite3.connect(db) as c:
  assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
  tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
  data={name:sorted(c.execute('SELECT * FROM "'+name+'"').fetchall(),key=repr) for name in tables}
  return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
pre=snapshot()
if phase=="before":
 convo=repo.create_conversation("miori");assert convo.conversation_id==identity(1)
 for n,kind in [(2,"completed"),(3,"interrupted"),(4,"privacy_skipped")]:
  t=repo.create_processing_turn("miori",convo.conversation_id,ProcessingTurnInput(sanitized_user_content="検証専用の入力"))
  if kind=="completed":repo.complete_turn("miori",convo.conversation_id,t.turn_id,sanitized_assistant_content="保存済みの回答")
  elif kind=="interrupted":repo.interrupt_turn("miori",convo.conversation_id,t.turn_id,sanitized_assistant_content="再生済みのprefix")
  else:repo.skip_processing_turn_for_privacy("miori",convo.conversation_id,t.turn_id,PrivacySkippedTurnInput(reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,sanitizer_version="fixture-v1",policy_version="fixture-v1"))
 repo.rename_conversation("miori",identity(1),"検証用の保存タイトル")
 other=repo.create_conversation("fixture-other")
 repo.archive_conversation("fixture-other",other.conversation_id)
else:
 assert repo.resume_conversation("miori",identity(1)).title=="検証用の保存タイトル"
 assert repo.get_turn("miori",identity(1),identity(2)).assistant_content=="保存済みの回答"
 interrupted=repo.get_turn("miori",identity(1),identity(3))
 assert interrupted.status.value=="interrupted" and interrupted.assistant_content=="再生済みのprefix"
 private=repo.get_turn("miori",identity(1),identity(4))
 assert private.status.value=="privacy_skipped" and private.user_content is None and private.assistant_content is None
 assert private.sanitizer_version==private.policy_version=="fixture-v1"
 assert repo.get_turn("fixture-other",identity(1),identity(2)) is None
 assert len(repo.list_archived_conversations("fixture-other"))==1
 if phase in ("rollback","verify"):
  assert repo.get_turn("miori",identity(1),identity(6)).assistant_content=="afterで保存"
 if phase=="verify":
  assert repo.get_turn("miori",identity(1),identity(7)).assistant_content=="rollbackで保存"
 else:
  t=repo.create_processing_turn("miori",identity(1),ProcessingTurnInput(sanitized_user_content="検証専用の追加入力"))
  repo.complete_turn("miori",identity(1),t.turn_id,sanitized_assistant_content=phase+"で保存")
turns=repo.list_turns("miori",identity(1))
expected={"before":3,"after":4,"rollback":5,"verify":5}[phase]
assert len(turns)==expected
print(json.dumps({"phase":phase,"schema_version":SCHEMA_VERSION,"turn_count":len(turns),"pre_rows_sha256":pre,"post_rows_sha256":snapshot(),"checks_passed":True}))
