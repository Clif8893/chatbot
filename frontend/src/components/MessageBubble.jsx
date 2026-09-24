import React from "react";

function formatTime(timestamp) {
  const date = new Date(timestamp);

  if (Number.isNaN(date.getTime())) {
    return "Now";
  }

  return new Intl.DateTimeFormat([], {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default function MessageBubble({ message }) {
  const isBot = message.sender === "bot";

  return (
    <div className={`message-row message-row--${message.sender}`}>
      {isBot && <div className="assistant-avatar" aria-hidden="true">🦷</div>}
      <article className={`message-bubble message-bubble--${message.sender}`}>
        {isBot && <span className="message-sender">Q&amp;M Assistant</span>}
        <p>{message.text}</p>
        {isBot && message.imageUrl && message.imageAlt && (
          <a
            className="message-attachment-link"
            href={message.imageUrl}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open larger view: ${message.imageAlt}`}
          >
            <img
              className="message-attachment-image"
              src={message.imageUrl}
              alt={message.imageAlt}
              loading="lazy"
            />
          </a>
        )}
        <time className="message-time" dateTime={message.timestamp}>
          {formatTime(message.timestamp)}
        </time>
      </article>
    </div>
  );
}
