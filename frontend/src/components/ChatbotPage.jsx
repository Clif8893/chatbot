import { useEffect, useRef, useState } from "react";

import ChatHeader from "./ChatHeader.jsx";
import ChatWindow from "./ChatWindow.jsx";
import MessageInput from "./MessageInput.jsx";
import { createConversation, getConversationMessages, listConversations, sendChatMessage } from "../services/chatApi.js";

const welcomeMessage = {
  id: "welcome-message",
  sender: "bot",
  text: "Hi there! 👋 I’m the Q&M Training Assistant. I can help with the Basic Certificate in Dental Assisting and the Certificate in Dental Clinic Front Desk Operations, or guide you through enrollment. How can I help you today?",
  timestamp: new Date().toISOString(),
};

// The id tells the assistant a button was pressed, so it can ask which course
// is meant when the conversation has not settled on one yet.
const quickActions = [
  { id: "about", label: "About the course", message: "Tell me about the course" },
  { id: "fee", label: "Course fee", message: "How much is the course fee?" },
  { id: "dates", label: "Available dates", message: "What course dates are available?" },
  { id: "enrol", label: "Enroll now", message: "I’d like to enroll" },
];

export function makeMessage(sender, text, timestamp = new Date().toISOString(), id, imageUrl, imageAlt, imageStatus) {
  return {
    id: id || `${sender}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    sender,
    text,
    timestamp,
    imageUrl,
    imageAlt,
    imageStatus,
  };
}

export function mapStoredMessages(messages) {
  if (!messages.length) return [welcomeMessage];
  return messages.map((message) => makeMessage(
    message.role === "assistant" ? "bot" : "user",
    message.content,
    message.timestamp,
    message.id,
    message.image_url,
    message.image_alt,
    message.image_status,
  ));
}

function maskNricForDisplay(text) {
  return text.replace(/\b([STFGM])\d{7}([A-Z])\b/gi, "$1*******$2");
}

export default function ChatbotPage({ userId, onConversationChange, onShowEnrollment }) {
  const storageKey = `qm-training-conversation-id:${userId}`;
  const [messages, setMessages] = useState([welcomeMessage]);
  const [conversationId, setConversationId] = useState("");
  const [isEnrollmentStarted, setIsEnrollmentStarted] = useState(false);
  const [isDemoMode, setIsDemoMode] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [previousChats, setPreviousChats] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const initializationStarted = useRef(false);

  useEffect(() => {
    if (initializationStarted.current) return;
    initializationStarted.current = true;
    async function initializeConversation() {
      const savedId = localStorage.getItem(storageKey);
      try {
        if (savedId) {
          const history = await getConversationMessages(savedId);
          setConversationId(savedId);
          onConversationChange?.(savedId);
          setMessages(mapStoredMessages(history.messages));
          setIsEnrollmentStarted(Boolean(history.enrollment?.status));
          setIsDemoMode(Boolean(history.enrollment?.demo_intakes_enabled));
          await refreshConversations();
          return;
        }
        await startNewConversation();
      } catch (requestError) {
        localStorage.removeItem(storageKey);
        try { await startNewConversation(); }
        catch (createError) { setError(createError.message || requestError.message); }
      } finally { setIsLoading(false); }
    }
    initializeConversation();
  }, []);

  async function startNewConversation() {
    setError("");
    setIsLoading(true);
    try {
      const result = await createConversation();
      localStorage.setItem(storageKey, result.conversation_id);
      setConversationId(result.conversation_id);
      onConversationChange?.(result.conversation_id);
      setMessages([{ ...welcomeMessage, timestamp: new Date().toISOString() }]);
      setIsEnrollmentStarted(false);
      setIsDemoMode(Boolean(result.demo_intakes_enabled));
      setHistoryOpen(false);
      await refreshConversations();
    } catch (requestError) { setError(requestError.message); }
    finally { setIsLoading(false); }
  }

  async function refreshConversations() {
    try {
      const result = await listConversations();
      setPreviousChats(result.conversations || []);
    } catch {
      setPreviousChats([]);
    }
  }

  async function reopenConversation(selectedId) {
    if (!selectedId || selectedId === conversationId) {
      setHistoryOpen(false);
      return;
    }
    setError("");
    setIsLoading(true);
    try {
      const history = await getConversationMessages(selectedId);
      localStorage.setItem(storageKey, selectedId);
      setConversationId(selectedId);
      onConversationChange?.(selectedId);
      setMessages(mapStoredMessages(history.messages));
      setIsEnrollmentStarted(Boolean(history.enrollment?.status));
      setIsDemoMode(Boolean(history.enrollment?.demo_intakes_enabled));
      setHistoryOpen(false);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSend(message, quickAction) {
    if (!conversationId) {
      setError("A chat could not be started. Please select New Chat and try again.");
      return;
    }
    setError("");
    setMessages((current) => [...current, makeMessage("user", maskNricForDisplay(message))]);
    setIsLoading(true);
    try {
      const response = await sendChatMessage(message, conversationId, quickAction);
      const reply = response.message;
      setMessages((current) => [...current, makeMessage(
        "bot",
        reply.content,
        reply.timestamp,
        reply.id,
        reply.image_url,
        reply.image_alt,
        reply.image_status,
      )]);
      setIsEnrollmentStarted(Boolean(response.enrollment?.status));
      setIsDemoMode(Boolean(response.enrollment?.demo_intakes_enabled));
      if (response.show_invoice_action || response.enrollment?.status === "confirmed") {
        onShowEnrollment?.();
      }
      await refreshConversations();
    } catch (requestError) { setError(requestError.message); }
    finally { setIsLoading(false); }
  }

  return <main className="chat-shell" aria-labelledby="chat-title">
    <div className="chat-top">
      <ChatHeader
        onNewChat={startNewConversation}
        onToggleHistory={() => setHistoryOpen((value) => !value)}
        historyOpen={historyOpen}
        disabled={isLoading}
        demoMode={isDemoMode}
      />
      {historyOpen && <section id="previous-chats" className="previous-chats" aria-label="Previous chats">
        <h2>Previous Chats</h2>
        {previousChats.length === 0
          ? <p>No earlier chats yet.</p>
          : <ul>{previousChats.map((chat) => <li key={chat.conversation_id}>
            <button type="button" onClick={() => reopenConversation(chat.conversation_id)} disabled={isLoading} aria-current={chat.conversation_id === conversationId ? "true" : undefined}>
              <strong>{formatConversationDate(chat.updated_at)}</strong>
              <span>{chat.preview}</span>
            </button>
          </li>)}</ul>}
      </section>}
    </div>
    <ChatWindow messages={messages} isLoading={isLoading} quickActions={isEnrollmentStarted ? [] : quickActions} onQuickAction={handleSend} />
    <MessageInput onSend={handleSend} isLoading={isLoading} error={error} />
  </main>;
}

function formatConversationDate(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "Saved chat";
  return new Intl.DateTimeFormat([], {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
