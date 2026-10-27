"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowLeft, Check, ChevronDown, Code2, FileCode2, Save, ShieldCheck, Sparkles, WandSparkles } from "lucide-react";
import { PageHeader, Panel, StatusBadge } from "@/components/ui";

const starterCode = `from vynmatrix import Strategy\nfrom vynmatrix.indicators import EMA, RSI\n\nclass EmaMomentum(Strategy):\n    def initialize(self):\n        self.fast_ema = EMA(12)\n        self.slow_ema = EMA(26)\n        self.rsi = RSI(14)\n\n    def on_data(self, bar):\n        # Draft logic: validate indicator API before running.\n        if self.fast_ema.crossed_above(self.slow_ema):\n            if self.rsi.value < 70:\n                self.emit_signal(\"BUY\")\n\n        elif self.fast_ema.crossed_below(self.slow_ema):\n            self.emit_signal(\"SELL\")`;

export default function NewStrategyPage() {
  const [name, setName] = useState("EMA Momentum + RSI");
  const [code, setCode] = useState(starterCode);
  const [showValidation, setShowValidation] = useState(false);
  const [strategyType, setStrategyType] = useState("Python strategy");
  return <>
    <PageHeader eyebrow="STRATEGY LAB / NEW" title="Create a strategy" description="Define your rules, validate the draft, and create a version you can backtest." actions={<Link className="button button-secondary" href="/strategies"><ArrowLeft size={15} /> Back to strategies</Link>} />
    <div className="builder-progress"><div className="builder-step current"><span>1</span><div><strong>Define</strong><small>Strategy logic</small></div></div><div className="builder-progress-line" /><div className="builder-step"><span>2</span><div><strong>Validate</strong><small>Rules & dependencies</small></div></div><div className="builder-progress-line" /><div className="builder-step"><span>3</span><div><strong>Backtest</strong><small>Historical evaluation</small></div></div></div>
    <div className="builder-layout">
      <div className="builder-main">
        <Panel title="Strategy configuration" subtitle="Set a clear identity for this strategy version.">
          <div className="form-grid"><label className="form-field full-field"><span>Strategy name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. EMA Momentum + RSI" /></label><label className="form-field"><span>Strategy format</span><div className="select-wrap"><select value={strategyType} onChange={(e) => setStrategyType(e.target.value)}><option>Python strategy</option><option>Visual rule builder (planned)</option><option>Import strategy (planned)</option></select><ChevronDown size={15} /></div></label><label className="form-field"><span>Target environment</span><div className="select-wrap"><select defaultValue="Paper trading"><option>Backtest only</option><option>Paper trading</option></select><ChevronDown size={15} /></div></label><label className="form-field"><span>Instrument universe</span><div className="select-wrap"><select defaultValue="NSE equities (sample)"><option>NSE equities (sample)</option><option>Index futures (sample)</option><option>Custom dataset</option></select><ChevronDown size={15} /></div></label><label className="form-field"><span>Default timeframe</span><div className="select-wrap"><select defaultValue="15 minutes"><option>1 minute</option><option>5 minutes</option><option>15 minutes</option><option>1 hour</option><option>1 day</option></select><ChevronDown size={15} /></div></label></div>
        </Panel>
        <Panel title="Strategy logic" subtitle="Python draft · editor integration can be upgraded to Monaco." actions={<span className="editor-language"><span /> PYTHON</span>}>
          <div className="editor-toolbar"><div><FileCode2 size={15} /><span>strategy.py</span><span className="editor-unsaved">Draft</span></div><button className="text-button" onClick={() => setCode(starterCode)}>Reset template</button></div>
          <textarea className="code-editor" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} aria-label="Python strategy editor" />
          <div className="editor-footer"><span><Code2 size={13} /> Python · Draft code only</span><span>{code.split("\n").length} lines</span></div>
        </Panel>
        {showValidation && <div className="validation-result"><div className="validation-icon"><Check size={17} /></div><div><strong>Draft checks completed locally</strong><p>Name and code are present. Engine-level syntax, API, indicator, and safety validation still require the backend.</p></div><StatusBadge tone="warning">Not engine-validated</StatusBadge></div>}
        <div className="builder-actions"><Link href="/strategies" className="button button-secondary">Cancel</Link><button className="button button-secondary" onClick={() => setShowValidation(true)}><ShieldCheck size={15} /> Validate draft</button><button className="button button-primary" onClick={() => setShowValidation(true)}><Save size={15} /> Save draft</button></div>
      </div>
      <aside className="builder-side">
        <Panel title="Build with AI" subtitle="Assistant-generated drafts must be reviewed before execution." className="ai-panel"><div className="ai-orb"><Sparkles size={24} /></div><h3>Describe your idea</h3><p>Start with plain language. Turn it into an editable, testable strategy draft.</p><textarea className="ai-prompt" placeholder="Example: Buy when EMA 12 crosses above EMA 26 and RSI is below 70..." /><button className="button button-primary button-full" onClick={() => setShowValidation(true)}><WandSparkles size={15} /> Prepare draft <span className="planned-label">DEMO</span></button><p className="micro-copy">Demo interaction only. Connect a secured AI endpoint to generate code.</p></Panel>
        <Panel title="Before you backtest" subtitle="Recommended validation checklist"><div className="checklist"><div><Check size={14} /><span>Define entry and exit behavior</span></div><div><Check size={14} /><span>Specify position sizing and risk caps</span></div><div><Check size={14} /><span>Account for fees and slippage</span></div><div><Check size={14} /><span>Prevent look-ahead and data leakage</span></div><div><Check size={14} /><span>Evaluate out-of-sample performance</span></div></div></Panel>
        <div className="builder-risk-note"><ShieldCheck size={16} /><p><strong>Safe by design</strong>Strategy code should run only in isolated workers with resource limits. Browser-side validation is not a security boundary.</p></div>
      </aside>
    </div>
  </>;
}
