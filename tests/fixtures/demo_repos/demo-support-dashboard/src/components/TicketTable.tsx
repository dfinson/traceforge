import type { Ticket } from "../types";

interface TicketTableProps {
  tickets: Ticket[];
}

export function TicketTable({ tickets }: TicketTableProps) {
  return (
    <div className="panel ticket-table">
      <div className="panel-header">
        <div>
          <h2>Active tickets</h2>
          <p>Support queue for the customer success rotation.</p>
        </div>
        <span className="table-count">{tickets.length} visible</span>
      </div>

      <table>
        <thead>
          <tr>
            <th>Ticket</th>
            <th>Customer</th>
            <th>Status</th>
            <th>Priority</th>
            <th>Owner</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {tickets.length === 0 ? (
            <tr>
              <td colSpan={6} className="empty-state">
                No tickets match your current filters. Try broadening your search or changing the status filter.
              </td>
            </tr>
          ) : null}
          {tickets.map((ticket) => (
            <tr key={ticket.id}>
              <td>
                <div className="ticket-subject">{ticket.subject}</div>
                <div className="ticket-id">{ticket.id}</div>
              </td>
              <td>
                <div>{ticket.accountName}</div>
                <div className="ticket-id">{ticket.customerEmail}</div>
              </td>
              <td>
                <span className={`status-badge status-${ticket.status}`}>{ticket.status}</span>
              </td>
              <td>
                <span className={`priority-badge priority-${ticket.priority}`}>{ticket.priority}</span>
              </td>
              <td>{ticket.owner}</td>
              <td>{ticket.updatedAt}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
