import React, { useEffect, useRef, useState } from "react";

export default function MessageInput({ onSend, isLoading, error, paymentProgress }) {
  const [message, setMessage] = useState("");
  const [validationError, setValidationError] = useState("");
  const [attachment, setAttachment] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [paymentType, setPaymentType] = useState("paynow");
  const input = useRef(null);
  const fileInput = useRef(null);
  const submitting = useRef(false);

  useEffect(() => {
    if (!isLoading) {
      submitting.current = false;
      input.current?.focus();
    }
  }, [isLoading]);

  function handleSubmit(event) {
    event.preventDefault();
    if (submitting.current || isLoading) return;
    const trimmedMessage = message.trim();

    if (!trimmedMessage && !attachment) {
      setValidationError("Please enter a message or attach a screenshot.");
      input.current?.focus();
      return;
    }

    setValidationError("");
    submitting.current = true;
    if (attachment) {
      onSend(trimmedMessage, null, attachment, crypto.randomUUID(), paymentType).then((success) => {
        if (success) {
          setMessage("");
          setAttachment(null);
          URL.revokeObjectURL(previewUrl);
          setPreviewUrl("");
          if (fileInput.current) fileInput.current.value = "";
        }
      });
    } else {
      setMessage("");
      onSend(trimmedMessage);
    }
  }

  function chooseFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
      setValidationError("Choose a PNG, JPEG or WebP screenshot under 5 MB.");
      event.target.value = "";
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setAttachment(file);
    setPreviewUrl(URL.createObjectURL(file));
    setValidationError("");
  }

  const visibleError = validationError || error;

  return (
    <form className="composer" onSubmit={handleSubmit} noValidate>
      {attachment && <div className="payment-attachment-preview">
        <img src={previewUrl} alt="Payment screenshot preview" />
        <div className="payment-attachment-info"><strong>{attachment.name}</strong><small>{(attachment.size / 1024).toFixed(0)} KB · Ready to upload</small></div>
        <select aria-label="Screenshot type" value={paymentType} onChange={(event) => setPaymentType(event.target.value)} disabled={isLoading}>
          <option value="paynow">PayNow</option>
          <option value="skillsfuture">SkillsFuture claim</option>
        </select>
        <button type="button" onClick={() => { URL.revokeObjectURL(previewUrl); setPreviewUrl(""); setAttachment(null); if (fileInput.current) fileInput.current.value = ""; }}>Remove</button>
      </div>}
      <button className="payment-attach-button" type="button" aria-label="Attach payment screenshot" title="Attach payment screenshot" onClick={() => fileInput.current?.click()} disabled={isLoading}>
        <svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m20.3 11.4-7.7 7.7a5.4 5.4 0 0 1-7.6-7.6l8.4-8.4a3.6 3.6 0 0 1 5.1 5.1L10 16.7a1.8 1.8 0 0 1-2.5-2.5l7.5-7.5" /></svg>
      </button>
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" onChange={chooseFile} disabled={isLoading} hidden />
      <label className="visually-hidden" htmlFor="message-input">
        Message
      </label>
      <input
        ref={input}
        id="message-input"
        name="message"
        type="text"
        value={message}
        placeholder={attachment ? "Add a message (optional)" : "Type a message"}
        autoComplete="off"
        aria-describedby="input-error"
        aria-invalid={Boolean(visibleError)}
        disabled={isLoading}
        onChange={(event) => {
          setMessage(event.target.value);
          setValidationError("");
        }}
      />
      <button type="submit" disabled={isLoading}>
        Send
      </button>
      <p className="input-error" id="input-error" role="alert" hidden={!visibleError}>
        {visibleError}
      </p>
      {paymentProgress && <div className="payment-progress" role="status"><span>Uploading and processing your screenshot…</span><span className="payment-progress-track" aria-hidden="true"><span /></span></div>}
    </form>
  );
}
