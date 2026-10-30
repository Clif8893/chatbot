import React, { useEffect, useState } from "react";

import ChatbotPage from "./components/ChatbotPage.jsx";
import ParticipantPanel from "./components/enrollment/ParticipantPanel.jsx";
import StaffPortal from "./components/StaffPortal.jsx";
import {
  createGuestSession,
  getAuthSession,
  setCsrfToken,
} from "./services/chatApi.js";
import "./workspace.css";

export default function App() {
  if (window.location.pathname.startsWith("/staff")) {
    return <div className="portal-root"><StaffPortal /></div>;
  }
  return <ParticipantPortal />;
}

function ParticipantPortal() {
  const [auth, setAuth] = useState(null);
  const [checking, setChecking] = useState(true);
  const [conversationId, setConversationId] = useState("");
  const [showEnrollment, setShowEnrollment] = useState(
    () => localStorage.getItem("qm-participant-enrollment-panel") === "open",
  );
  const [statusRevision, setStatusRevision] = useState(0);

  useEffect(() => {
    getAuthSession()
      .then((result) => {
        setCsrfToken(result.csrf_token);
        setAuth(result.authenticated ? result : null);
      })
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    localStorage.setItem(
      "qm-participant-enrollment-panel",
      showEnrollment ? "open" : "closed",
    );
  }, [showEnrollment]);

  if (checking) {
    return <div className="portal-root"><main className="guest-entry-page"><p>Opening the assistant…</p></main></div>;
  }
  if (!auth) {
    return <div className="portal-root"><ParticipantNameEntry onReady={(result) => {
      setCsrfToken(result.csrf_token);
      setAuth(result);
    }} /></div>;
  }

  return <div className="portal-root">
    <nav className="public-navigation" aria-label="Participant chat">
      <strong>Q&amp;M Academy</strong>
      <span>
        <button type="button" className="text-button" onClick={() => setShowEnrollment((value) => !value)}>{showEnrollment ? "Return to chat" : "Enrollment & invoice"}</button>
        {" · "}Hi, {auth.user.full_name}
      </span>
    </nav>
    <div className={`participant-workspace${showEnrollment ? " participant-workspace--expanded" : ""}`}>
      <ChatbotPage
        userId={auth.user.id}
        onConversationChange={setConversationId}
        onShowEnrollment={() => {
          setShowEnrollment(true);
          setStatusRevision((value) => value + 1);
        }}
      />
      {showEnrollment && <ParticipantPanel conversationId={conversationId} revision={statusRevision} onUpdated={() => setStatusRevision((value) => value + 1)} />}
    </div>
  </div>;
}

function ParticipantNameEntry({ onReady }) {
  const [fullName, setFullName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await createGuestSession(fullName);
      onReady(result);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  return <main className="guest-entry-page">
    <div className="guest-entry-intro">
      <a href="/" className="brand-link">Q&amp;M Academy</a>
      <span className="eyebrow">Training assistant</span>
      <h1>Chat with us about your course.</h1>
      <p>No account or password is needed. Enter your name and start chatting, just like a messaging conversation.</p>
      <a className="back-link" href="/staff">Staff workspace →</a>
    </div>
    <section className="guest-entry-card" aria-labelledby="guest-entry-title">
      <span className="eyebrow">Welcome</span>
      <h2 id="guest-entry-title">What should we call you?</h2>
      <p className="muted">Your name identifies this chat session. No email or password is required.</p>
      <form className="stack-form" onSubmit={submit}>
        <label>Your name<input autoFocus autoComplete="name" maxLength="80" placeholder="e.g. Dhiraj" value={fullName} onChange={(event) => setFullName(event.target.value)} required /></label>
        {error && <p className="notice-error" role="alert">{error}</p>}
        <button className="primary-button" disabled={busy} type="submit">{busy ? "Starting chat…" : "Start chat"}</button>
      </form>
    </section>
  </main>;
}
