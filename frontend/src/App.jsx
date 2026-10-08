import { useEffect, useState } from "react";

import ChatbotPage from "./components/ChatbotPage.jsx";
import ParticipantPanel from "./components/enrollment/ParticipantPanel.jsx";
import StaffPortal from "./components/StaffPortal.jsx";
import {
  getAuthSession,
  loginParticipant,
  logoutParticipant,
  registerParticipant,
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

  async function logout() {
    await logoutParticipant();
    setCsrfToken("");
    setAuth(null);
    setShowEnrollment(false);
  }

  if (checking) {
    return <div className="portal-root"><main className="login-page"><p>Loading your account…</p></main></div>;
  }
  if (!auth) {
    return <div className="portal-root"><ParticipantAuth onAuthenticated={(result) => {
      setCsrfToken(result.csrf_token);
      setAuth(result);
    }} /></div>;
  }

  return <div className="portal-root">
    <nav className="public-navigation" aria-label="Participant portal">
      <strong>Q&amp;M Academy</strong>
      <span>
        <button type="button" className="text-button" onClick={() => setShowEnrollment((value) => !value)}>{showEnrollment ? "Return to chat" : "Enrollment & payment"}</button>
        {" · "}{auth.user.full_name}{" · "}<button type="button" className="text-button" onClick={logout}>Sign out</button>
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

function ParticipantAuth({ onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = mode === "register"
        ? await registerParticipant(fullName, email, password)
        : await loginParticipant(email, password);
      onAuthenticated(result);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  return <main className="login-page">
    <div className="login-intro">
      <a href="/" className="brand-link">Q&amp;M Academy</a>
      <span className="eyebrow">Participant portal</span>
      <h1>Course support, all in one place.</h1>
      <p>Ask course questions, continue your enrollment and return to your saved conversation.</p>
      <a className="back-link" href="/staff">Staff sign in →</a>
    </div>
    <section className="login-card" aria-labelledby="participant-auth-title">
      <span className="eyebrow">{mode === "register" ? "Create account" : "Welcome back"}</span>
      <h2 id="participant-auth-title">{mode === "register" ? "Register for the portal" : "Sign in to continue"}</h2>
      <form className="stack-form" onSubmit={submit}>
        {mode === "register" && <label>Full name<input autoComplete="name" value={fullName} onChange={(event) => setFullName(event.target.value)} required /></label>}
        <label>Email<input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
        <label>Password<input type="password" autoComplete={mode === "register" ? "new-password" : "current-password"} minLength="8" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        {error && <p className="notice-error" role="alert">{error}</p>}
        <button className="primary-button" disabled={busy} type="submit">{busy ? "Please wait…" : mode === "register" ? "Create account" : "Sign in"}</button>
      </form>
      <button className="text-button auth-mode-button" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
        {mode === "login" ? "New participant? Create an account" : "Already registered? Sign in"}
      </button>
    </section>
  </main>;
}
