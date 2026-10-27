import type { ReactNode } from "react";

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="header-actions">{actions}</div>}
    </div>
  );
}

export function Panel({ id, title, subtitle, actions, children, className = "" }: { id?: string; title: string; subtitle?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section id={id} className={`panel ${className}`}>
      <div className="panel-header">
        <div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
        {actions && <div className="panel-actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export function MetricCard({ label, value, change, icon, tone = "blue" }: { label: string; value: string; change?: string; icon: ReactNode; tone?: "blue" | "green" | "purple" | "orange" }) {
  return (
    <div className="metric-card">
      <div className="metric-top"><span>{label}</span><span className={`metric-icon ${tone}`}>{icon}</span></div>
      <div className="metric-value">{value}</div>
      {change && <div className="metric-change">{change}</div>}
    </div>
  );
}

export function StatusBadge({ children, tone = "neutral" }: { children: ReactNode; tone?: "success" | "warning" | "danger" | "info" | "neutral" }) {
  return <span className={`status-badge ${tone}`}><span className="status-dot" />{children}</span>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <div className="empty-state"><div className="empty-state-mark">VM</div><h3>{title}</h3><p>{description}</p></div>;
}
