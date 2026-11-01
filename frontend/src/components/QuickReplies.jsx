import React from "react";

// Suggested replies come from the assistant; choosing one sends its label as
// an ordinary message.
export default function QuickReplies({ replies, onSelect }) {
  return (
    <nav className="quick-actions" aria-label="Suggested replies">
      {replies.map((label) => (
        <button key={label} type="button" onClick={() => onSelect(label)}>
          {label}
        </button>
      ))}
    </nav>
  );
}
