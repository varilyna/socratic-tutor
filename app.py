import json
import os
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from tutor.engine import finish, prepare, start, turn
from tutor.material import read_upload
from tutor.provider import GigaChatClient, ModelError, load_env


ROOT = Path(__file__).resolve().parent
load_env(str(ROOT / ".env"))
st.set_page_config(page_title="Сократический тьютор", layout="centered")
st.html("""
<style>
[data-testid="stFileUploaderDropzoneInstructions"] {
    display: none;
}
</style>
""")
st.title("Учиться через диалог")
st.caption("Загрузите материал, разберитесь в нем на примерах и проверьте понимание.")


def get_client():
    settings = (
        os.getenv("GIGACHAT_AUTH_KEY", ""), os.getenv("GIGACHAT_MODEL", "GigaChat"),
        os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1"),
        os.getenv("GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"),
        os.getenv("GIGACHAT_CA_BUNDLE", ""),
    )
    if st.session_state.get("client_settings") != settings:
        client = GigaChatClient(settings[0], settings[1], scope=settings[2],
                                base_url=settings[3], auth_url=settings[4], ca_bundle=settings[5])
        st.session_state.client = client
        st.session_state.client_settings = settings
    return st.session_state.client


with st.sidebar:
    st.header("Как это работает")
    st.write(
        "Загрузите или вставьте текст, который уже изучили. Тьютор выделит основные темы "
        "и предложит вопросы и задания, чтобы вы могли проверить своё понимание. "
        "Если будет сложно, он даст подсказку или объяснение, а затем предложит "
        "новый пример для самостоятельного решения."
    )
    # connected = bool(os.getenv("GIGACHAT_AUTH_KEY"))
    # if connected:
    #     st.success("Ключ задан локально. Доступ проверится при первом запросе.")
    # else:
    #     st.info("Для работы модели нужен ключ GigaChat в файле .env. Инструкция — в README.md.")
    debug = False
    # debug = st.checkbox("Показать логику диалога", value=False)
    if st.button("Начать заново", use_container_width=True):
        for key in ("plan", "material", "session", "pending_answer", "upload_signature"):
            st.session_state.pop(key, None)
        st.rerun()
    # if "client" in st.session_state:
    #     st.caption(f"Запросов к модели: {st.session_state.client.calls} · токенов: {st.session_state.client.total_tokens}")

session = st.session_state.get("session")
if session is None:
    st.subheader("1. Учебный материал")
    if st.button("Взять пример OKR из задания"):
        st.session_state.input_material = (ROOT / "data/okr.txt").read_text(encoding="utf-8")
        st.session_state.pop("plan", None)
    upload = st.file_uploader("Или загрузите TXT, Markdown, DOCX", type=["txt", "md", "docx"])
    if upload is not None:
        signature = (upload.name, upload.size, upload.file_id)
        if signature != st.session_state.get("upload_signature"):
            try:
                st.session_state.input_material = read_upload(upload.getvalue(), upload.name)
                st.session_state.upload_signature = signature
                st.session_state.pop("plan", None)
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    material = st.text_area("Вставьте текст, который уже прочитали", key="input_material", height=270,
                            placeholder="Статья, конспект или фрагмент учебника…")
    if st.button("Подготовить обучение", type="primary", disabled=not material.strip()):
        try:
            with st.spinner("Выделяю понятия и готовлю задания по вашему тексту…"):
                plan = prepare(get_client(), material)
            st.session_state.plan = plan
            st.session_state.material = material.strip()
        except (ModelError, ValueError) as exc:
            st.error(str(exc))
    plan = st.session_state.get("plan")
    if plan:
        if material.strip() != st.session_state.material:
            st.warning("Текст изменился. Подготовьте обучение заново, чтобы план соответствовал материалу.")
        else:
            st.subheader("2. Что будем осваивать")
            st.write(plan["title"])
            for goal in plan["goals"]:
                with st.expander(goal["name"]):
                    st.write(goal["criterion"])
                    st.caption("Основание в материале")
                    st.write(goal["source_quote"])
            st.caption("Если цели не подходят, уточните учебный текст и подготовьте план заново.")
            if st.button("Начать диалог", type="primary"):
                st.session_state.session = start(st.session_state.material, plan)
                st.rerun()
else:
    with st.expander("Материал и учебные цели"):
        st.write(session.plan["title"])
        for goal in session.plan["goals"]:
            st.write(f"• {goal['name']}")
        st.text(session.material)
    completed = sum(v == "demonstrated_independently" for v in session.statuses.values())
    st.progress(completed / len(session.statuses),
                text=f"Самостоятельно проверено навыков: {completed} из {len(session.statuses)}")
    if not session.finished:
        stage = "Первый пример" if session.task["kind"] == "diagnostic" else "Новый пример"
        if session.assisted:
            stage = "Практика с помощью"
        st.caption(f"{session.goal['name']} · {stage}")
    for message in session.history:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    pending = st.session_state.get("pending_answer")
    if pending:
        st.warning("Предыдущий ответ не удалось обработать. Он сохранен для повторной попытки.")
        st.write(pending)
    if not session.finished:
        left, right = st.columns(2)
        answer = st.chat_input("Ваш ответ или вопрос", max_chars=4000)
        if left.button("Нужна подсказка", use_container_width=True):
            answer = "Мне нужна подсказка к текущему заданию."
        if right.button("Завершить с итогом", use_container_width=True):
            st.session_state.session = finish(session)
            st.session_state.pop("pending_answer", None)
            st.rerun()
        if pending and st.button("Повторить обработку ответа"):
            answer = pending
        if answer:
            try:
                with st.spinner("Разбираю ваш ответ и выбираю следующий шаг…"):
                    updated = turn(get_client(), session, answer)
                st.session_state.session = updated
                st.session_state.pop("pending_answer", None)
                st.rerun()
            except (ModelError, ValueError) as exc:
                st.session_state.pending_answer = answer
                st.error(str(exc))
    else:
        st.download_button("Скачать диалог и результаты", data=json.dumps({
            "topic": session.plan["title"], "statuses": session.statuses,
            "dialogue": session.history, "evidence": session.evidence,
        }, ensure_ascii=False, indent=2), file_name="learning_session.json", mime="application/json")
    if debug:
        with st.expander("Состояние и решения контроллера", expanded=True):
            st.json({"goal_id": session.goal["id"], "task_id": session.task["id"],
                     "help_level": session.help_level, "turns": session.turns,
                     "statuses": session.statuses, "finished": session.finished,
                     "finish_reason": session.finish_reason})
            st.json(session.trace[-5:])
            st.download_button("Скачать трассу для проверки", json.dumps(asdict(session), ensure_ascii=False, indent=2),
                               file_name="tutor_trace.json", mime="application/json")
