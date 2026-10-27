"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, useEffect } from "react";
import {
  Activity, Bell, Blocks, ChevronDown, CircleHelp, Command, LayoutDashboard, Menu, Moon,
  Search, Settings2, ShieldCheck, TrendingUp, X,
} from "lucide-react";
import type { ReactNode } from "react";
import { LockButton } from "./login-modal";
import { api } from "@/lib/api-client";
import type { OverviewData } from "@/lib/types";

const primaryNavigation = [
  { label: "Overview", href: "/overview", icon: LayoutDashboard },
  { label: "Strategy Lab", href: "/strategies", icon: Blocks },
  { label: "Profit & Loss", href: "/pnl", icon: TrendingUp },
];
const platformNavigation = [
  { label: "Audit & Feedback", href: "/activity", icon: ShieldCheck },
  { label: "Settings", href: "/settings", icon: Settings2 },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [backendConnected, setBackendConnected] = useState<boolean | null>(null);

  useEffect(() => {
    async function checkBackend() {
      try {
        const data = await api.overview();
        setOverview(data);
        setBackendConnected(true);
      } catch {
        setBackendConnected(false);
      }
    }
    checkBackend();
  }, []);

  function navLink(item: (typeof primaryNavigation)[number] | (typeof platformNavigation)[number]) {
    const active = pathname === item.href || (item.href !== "/overview" && pathname.startsWith(`${item.href}/`));
    const Icon = item.icon;
    return (
      <Link key={item.href} className={`nav-link ${active ? "active" : ""}`} href={item.href} onClick={() => setMobileOpen(false)}>
        <Icon size={17} strokeWidth={1.8} />
        <span>{item.label}</span>
      </Link>
    );
  }

  return (
    <div className="app-shell">
      {mobileOpen && <button className="mobile-backdrop" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-symbol"><Activity size={21} strokeWidth={2.2} /></div>
          <div className="brand-copy"><strong>vynmatrix</strong><span>QUANT WORKSPACE</span></div>
          <button className="icon-button mobile-close" aria-label="Close navigation" onClick={() => setMobileOpen(false)}><X size={17} /></button>
        </div>
        <div className="workspace-switcher"><div className="workspace-avatar">D</div><div className="workspace-copy"><strong>Personal workspace</strong><span>Self-hosted instance</span></div><ChevronDown size={15} /></div>
        <div className="nav-section-label">WORKSPACE</div>
        <nav className="nav-list">{primaryNavigation.map(navLink)}</nav>
        <div className="nav-section-label platform-label">PLATFORM</div>
        <nav className="nav-list">{platformNavigation.map(navLink)}</nav>
        <div className="sidebar-spacer" />
        <div className="risk-mini-card">
          <div className="risk-mini-title">
            <span className={`health-dot ${backendConnected === true ? "health-ok" : backendConnected === false ? "health-error" : "health-unknown"}`} />
            ENGINE STATUS
          </div>
          <strong>
            {backendConnected === true ? "API reachable" : backendConnected === false ? "API unavailable" : "Checking API..."}
          </strong>
          <p>Worker health is not checked. Status is from the last API request.</p>
          <div className="health-bars"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
        </div>
        <div className="sidebar-footer"><div className="avatar">DY</div><div className="user-copy"><strong>Workspace owner</strong><span>Administrator</span></div><button className="icon-button" aria-label="Help"><CircleHelp size={17} /></button></div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileOpen(true)}><Menu size={20} /></button>
          <div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{getCurrentLabel(pathname)}</strong></div>
          <div className="topbar-actions">
            <button className="search-trigger" onClick={() => setSearchOpen(!searchOpen)}><Search size={15} /><span>Search anything...</span><kbd><Command size={11} /> K</kbd></button>
            <span className="topbar-divider" />
            <span className="mode-pill">
              <span className={overview?.safety.execution_mode === "paper" && !overview.safety.allow_live ? "paper-dot" : ""} />
              {overview?.safety.execution_mode === "paper" && !overview.safety.allow_live ? "PAPER MODE" : overview?.safety.execution_mode.toUpperCase() || "UNKNOWN"}
            </span>
            <button className="icon-button" aria-label="Notifications"><Bell size={17} /><i className="notification-dot" /></button>
            <LockButton />
            <button className="icon-button theme-button" aria-label="Theme"><Moon size={17} /></button>
          </div>
        </header>
        {searchOpen && <div className="search-popover"><Search size={16} /><input autoFocus placeholder="Search strategies, backtests, events..." onKeyDown={(e) => { if (e.key === "Escape") setSearchOpen(false); }} /><kbd>ESC</kbd></div>}
        <main className="main-content">{children}</main>
        <footer className="app-footer"><span>Vynmatrix <span className="footer-muted">· Self-hosted quantitative research</span></span><span>Owner API · Recorded data</span></footer>
      </div>
    </div>
  );
}

function getCurrentLabel(pathname: string) {
  if (pathname.startsWith("/strategies")) return "Strategy Lab";
  if (pathname.startsWith("/pnl")) return "Profit & Loss";
  if (pathname.startsWith("/activity")) return "Audit & Feedback";
  if (pathname.startsWith("/settings")) return "Settings";
  return "Overview";
}
