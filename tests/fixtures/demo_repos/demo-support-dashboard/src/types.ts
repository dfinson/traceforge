export type TicketStatus = "open" | "investigating" | "waiting" | "resolved";

export type TicketPriority = "low" | "medium" | "high";

export interface Ticket {
  id: string;
  subject: string;
  customerEmail: string;
  accountName: string;
  priority: TicketPriority;
  status: TicketStatus;
  owner: string;
  updatedAt: string;
}
