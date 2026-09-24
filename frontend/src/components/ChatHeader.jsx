import React from "react";


export default function ChatHeader({ onNewChat, onToggleHistory, historyOpen = false, disabled, demoMode = false }) {
  return (
    <header className="chat-header">
      <div className="brand-mark" aria-hidden="true">
        Q
      </div>
      <div className="header-copy">
        <h1 id="chat-title">Q&amp;M Training</h1>
        <p>Course Enquiry &amp; Enrollment Assistant</p>
      </div>
      <div className="header-actions">
        <span className="online-status">
          <span aria-hidden="true">●</span> Online
        </span>
        <button
          className="new-conversation-button"
          type="button"
          onClick={onToggleHistory}
          disabled={disabled}
          aria-expanded={historyOpen}
          aria-controls="previous-chats"
        >
          Previous Chats
        </button>
        <button
          className="new-conversation-button"
          type="button"
          onClick={onNewChat}
          disabled={disabled}
        >
          New Chat
        </button>
      </div>
    </header>
  );
}
