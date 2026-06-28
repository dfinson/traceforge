import { useState, type FormEvent } from "react";

import type { Ticket } from "../types";

interface NewTicketFormProps {
  onCreate: (ticket: Ticket) => void;
}

type FieldName = "subject" | "customerEmail";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function validate(subject: string, customerEmail: string): Record<FieldName, string> {
  return {
    subject: subject.trim() ? "" : "Subject is required.",
    customerEmail: !customerEmail.trim()
      ? "Customer email is required."
      : !EMAIL_RE.test(customerEmail.trim())
        ? "Enter a valid email address."
        : "",
  };
}

export function NewTicketForm({ onCreate }: NewTicketFormProps) {
  const [subject, setSubject] = useState("");
  const [customerEmail, setCustomerEmail] = useState("");
  const [accountName, setAccountName] = useState("");

  const [touched, setTouched] = useState<Partial<Record<FieldName, true>>>({});
  const [submitted, setSubmitted] = useState(false);

  const errors = validate(subject, customerEmail);

  function showError(field: FieldName) {
    return (touched[field] || submitted) && errors[field] ? errors[field] : "";
  }

  function blur(field: FieldName) {
    setTouched((prev) => ({ ...prev, [field]: true }));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitted(true);

    if (Object.values(errors).some(Boolean)) {
      return;
    }

    onCreate({
      id: `SUP-${Math.floor(Math.random() * 1000) + 2000}`,
      subject: subject.trim(),
      customerEmail: customerEmail.trim(),
      accountName: accountName.trim() || "New account",
      priority: "medium",
      status: "open",
      owner: "Unassigned",
      updatedAt: "just now",
    });

    setSubject("");
    setCustomerEmail("");
    setAccountName("");
    setTouched({});
    setSubmitted(false);
  }

  return (
    <form className="panel form-panel" onSubmit={handleSubmit}>
      <div className="panel-header">
        <div>
          <h2>Create ticket</h2>
          <p>Quick intake form for the support desk.</p>
        </div>
      </div>

      <label className={showError("subject") ? "field-invalid" : ""}>
        Subject
        <input
          value={subject}
          onChange={(event) => setSubject(event.target.value)}
          onBlur={() => blur("subject")}
          placeholder="Webhook retries spike after deploy"
          aria-invalid={!!showError("subject")}
        />
        {showError("subject") && <span className="field-error">{showError("subject")}</span>}
      </label>

      <label className={showError("customerEmail") ? "field-invalid" : ""}>
        Customer email
        <input
          value={customerEmail}
          onChange={(event) => setCustomerEmail(event.target.value)}
          onBlur={() => blur("customerEmail")}
          placeholder="ops@example.com"
          type="email"
          aria-invalid={!!showError("customerEmail")}
        />
        {showError("customerEmail") && <span className="field-error">{showError("customerEmail")}</span>}
      </label>

      <label>
        Account name
        <input value={accountName} onChange={(event) => setAccountName(event.target.value)} placeholder="Northwind" />
      </label>

      <button type="submit">Create ticket</button>
    </form>
  );
}
