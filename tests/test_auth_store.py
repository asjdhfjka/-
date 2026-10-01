"""轻量用户、会话和个人记录隔离测试。"""

import pytest

from auth_store import AuthStore


def test_create_authenticate_and_session_round_trip(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    user = store.create_user("alice", "StrongPass123!", "Alice", "student", "计算机学院")
    assert user["role"] == "student"
    assert "password_hash" not in user
    assert store.authenticate("alice", "wrong-password") is None
    assert store.authenticate("alice", "StrongPass123!")["id"] == user["id"]

    token = store.create_session(user["id"], days=1)
    assert store.user_for_session(token)["username"] == "alice"
    store.delete_session(token)
    assert store.user_for_session(token) is None


def test_duplicate_user_and_invalid_role_are_rejected(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "StrongPass123!", "Alice")
    with pytest.raises(ValueError, match="已经存在"):
        store.create_user("ALICE", "StrongPass123!", "Other")
    with pytest.raises(ValueError, match="角色"):
        store.create_user("bob", "StrongPass123!", "Bob", "root")


def test_bootstrap_users_is_idempotent(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    assert len(store.bootstrap_demo_users("Demo@123456")) == 3
    assert store.bootstrap_demo_users("Demo@123456") == []
    assert {user["role"] for user in store.list_users()} == {"student", "reviewer", "admin"}


def test_disabling_user_revokes_existing_sessions(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    user = store.create_user("alice", "StrongPass123!", "Alice")
    token = store.create_session(user["id"])
    store.set_user_active(user["id"], False)
    assert store.authenticate("alice", "StrongPass123!") is None
    assert store.user_for_session(token) is None


def test_chat_and_review_history_are_isolated_by_user(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    alice = store.create_user("alice", "StrongPass123!", "Alice")
    bob = store.create_user("bob", "StrongPass123!", "Bob")
    store.record_chat(alice["id"], "问题", "答案", [{"title": "制度"}])
    store.record_review(alice["id"], "材料.docx", {
        "材料类型": "奖学金评定", "审查结论": "需修改", "总评分": 84,
    })

    assert store.recent_chats(alice["id"])[0]["question"] == "问题"
    assert store.recent_chats(bob["id"]) == []
    assert store.recent_reviews(alice["id"])[0]["result"]["总评分"] == 84
    assert store.recent_reviews(alice["id"])[0]["result"]["记录ID"]
    assert store.recent_reviews(bob["id"]) == []


def test_material_submission_workflow_and_visibility(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    student = store.create_user("student1", "StrongPass123!", "学生甲")
    reviewer = store.create_user("reviewer1", "StrongPass123!", "审核员甲", "reviewer")
    review_id = store.record_review(
        student["id"], "奖学金材料.docx",
        {"材料类型": "奖学金评定", "审查结论": "需修改", "总评分": 82},
        str(tmp_path / "material.docx"),
    )

    submitted = store.submit_review(review_id, student["id"])
    assert submitted["status"] == "submitted"
    assert submitted["result"]["记录ID"] == review_id
    assert store.list_submissions(student["id"])[0]["student_name"] == "学生甲"
    assert store.list_submissions()[0]["review_id"] == review_id

    processed = store.update_submission(
        submitted["id"], "changes_requested", reviewer["id"], "请补充盖章页")
    assert processed["status"] == "changes_requested"
    assert processed["reviewer_name"] == "审核员甲"
    assert processed["reviewer_note"] == "请补充盖章页"
    assert store.recent_reviews(student["id"])[0]["submission_status"] == "changes_requested"

    resubmitted = store.submit_review(review_id, student["id"])
    assert resubmitted["id"] == submitted["id"]
    assert resubmitted["status"] == "submitted"
    assert resubmitted["reviewer_note"] == ""


def test_submission_rejects_foreign_review_and_invalid_status(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    alice = store.create_user("alice", "StrongPass123!", "Alice")
    bob = store.create_user("bob", "StrongPass123!", "Bob")
    reviewer = store.create_user("reviewer", "StrongPass123!", "Reviewer", "reviewer")
    review_id = store.record_review(
        alice["id"], "材料.pdf", {"材料类型": "通用", "审查结论": "通过", "总评分": 95},
        str(tmp_path / "材料.pdf"))

    with pytest.raises(ValueError, match="不属于"):
        store.submit_review(review_id, bob["id"])
    submission = store.submit_review(review_id, alice["id"])
    with pytest.raises(ValueError, match="状态"):
        store.update_submission(submission["id"], "deleted", reviewer["id"])
