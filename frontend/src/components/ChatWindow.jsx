import React, { useEffect, useRef } from "react";

import MessageBubble from "./MessageBubble.jsx";
import QuickReplies from "./QuickReplies.jsx";

export default function ChatWindow({
  messages,
  isLoading,
  quickReplies,
  onQuickReply,
}) {
  const latestMessage = useRef(null);

  useEffect(() => {
    latestMessage.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  return (
    <section
      className="conversation"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Chat conversation"
      aria-busy={isLoading}
    >
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}

      {isLoading && (
        <div className="message-row message-row--bot">
          <div className="assistant-avatar" aria-hidden="true">🦷</div>
          <div className="message-bubble message-bubble--bot loading-bubble">
            <span className="visually-hidden">Waiting for a response</span>
            <span className="loading-dot" />
            <span className="loading-dot" />
            <span className="loading-dot" />
          </div>
        </div>
      )}

      {!isLoading && quickReplies.length > 0 && (
        <QuickReplies replies={quickReplies} onSelect={onQuickReply} />
      )}

      <div ref={latestMessage} />
    </section>
  );
}
