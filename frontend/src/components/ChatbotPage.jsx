import React, { useEffect, useRef, useState } from "react";

import ChatHeader from "./ChatHeader.jsx";
import ChatWindow from "./ChatWindow.jsx";
import MessageInput from "./MessageInput.jsx";
import { apiBaseUrl, createConversation, getConversationMessages, listConversations, sendChatMessage, uploadEvidence } from "../services/chatApi.js";

const welcomeMessage = {
  id: "welcome-message",
  sender: "bot",
  text: "Hi there! 👋 I’m the Q&M Training Assistant. I can help with the Basic Certificate in Dental Assisting and the Certificate in Dental Clinic Front Desk Operations, or guide you through enrollment. How can I help you today?",
  timestamp: new Date().toISOString(),
};

const REMINDER_POLL_MS = 60_000;

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
    message.image_url?.startsWith("/api/") ? `${apiBaseUrl}${message.image_url}` : message.image_url,
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
  // Suggestions from the assistant's latest reply (or the starter menu).
  const [quickReplies, setQuickReplies] = useState([]);
  const [isDemoMode, setIsDemoMode] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [paymentProgress, setPaymentProgress] = useState(false);
  const [error, setError] = useState("");
  const [previousChats, setPreviousChats] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const initializationStarted = useRef(false);
  const busy = useRef(false);
  busy.current = isLoading;

  // Follow-up reminders are posted by the server while the chat is idle, so
  // check for new messages periodically and show them without a refresh.
  useEffect(() => {
    if (!conversationId) return undefined;
    const timer = setInterval(async () => {
      if (busy.current || document.hidden) return;
      try {
        const history = await getConversationMessages(conversationId);
        const stored = history.messages || [];
        if (busy.current || !stored.length) return;
        setMessages((current) => {
          if (current.some((message) => message.id === stored[stored.length - 1].id)) return current;
          // The greeting is shown by the page, not stored; keep it on screen.
          const greeting = current[0]?.id === welcomeMessage.id ? [current[0]] : [];
          return [...greeting, ...mapStoredMessages(stored)];
        });
      } catch {
        // A missed check is retried on the next interval.
      }
    }, REMINDER_POLL_MS);
    return () => clearInterval(timer);
  }, [conversationId]);

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
          setQuickReplies(history.quick_replies || []);
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
      setQuickReplies(result.quick_replies || []);
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
      setQuickReplies(history.quick_replies || []);
      setIsDemoMode(Boolean(history.enrollment?.demo_intakes_enabled));
      setHistoryOpen(false);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSend(message, attachment, submissionId, paymentType = "paynow") {
    if (!conversationId) {
      setError("A chat could not be started. Please select New Chat and try again.");
      return false;
    }
    setError("");
    setQuickReplies([]);
    setMessages((current) => [...current, makeMessage("user", attachment ? `${message || "Payment screenshot"} · Uploading…` : maskNricForDisplay(message))]);
    setIsLoading(true);
    if (attachment) setPaymentProgress(true);
    try {
      if (attachment) {
        const result = await uploadEvidence(conversationId, attachment, paymentType, message, submissionId);
        setMessages(mapStoredMessages(result.messages || []));
        await refreshConversations();
        return true;
      }
      const response = await sendChatMessage(message, conversationId);
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
      setQuickReplies(response.quick_replies || []);
      setIsDemoMode(Boolean(response.enrollment?.demo_intakes_enabled));
      if (response.show_invoice_action || response.enrollment?.status === "confirmed") {
        onShowEnrollment?.();
      }
      await refreshConversations();
    } catch (requestError) {
      if (attachment) setMessages((current) => current.slice(0, -1));
      setError(requestError.message);
      return false;
    }
    finally { setIsLoading(false); setPaymentProgress(false); }
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
    <ChatWindow messages={messages} isLoading={isLoading} quickReplies={quickReplies} onQuickReply={handleSend} />
    <MessageInput onSend={handleSend} isLoading={isLoading} error={error} paymentProgress={paymentProgress} />
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
