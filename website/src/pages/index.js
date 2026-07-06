import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './index.module.css';

const STAGES = ['Source', 'Parser', 'Adapter', 'Enricher', 'Pipeline', 'Sink(s)'];

const FEATURES = [
  {
    title: 'Framework-agnostic',
    body: 'Support a new agent framework by writing a single YAML mapping — no Python required. 16+ frameworks ship out of the box.',
  },
  {
    title: 'CPU-only, no torch',
    body: 'Live phase, boundary, and title structuring runs on packaged ONNX models. No GPU, no heavyweight ML stack.',
  },
  {
    title: 'Classification & risk',
    body: 'Multi-dimensional classification with tree-sitter shell AST analysis and a 0–100 risk score mapped to MITRE ATT&CK.',
  },
  {
    title: 'Governance built in',
    body: 'Data labeling, information-flow control, drift and budget tracking, and rule-driven recommendations — with an opt-in gate layer.',
  },
  {
    title: 'Eight storage sinks',
    body: 'JSONL, SQLite, Parquet, S3, OTLP, webhook, console, and callback sinks — all configurable from YAML with error isolation.',
  },
  {
    title: 'Observation-first',
    body: 'Observes, enriches, and recommends by default — never touching agent behavior. Enforcement is a separate, opt-in gate layer you switch on when you want it.',
  },
];

function Hero() {
  const logo = useBaseUrl('/img/logo.png');
  return (
    <header className={styles.hero}>
      <div className={styles.heroGlow} aria-hidden="true" />
      <div className={clsx('container', styles.heroInner)}>
        <img src={logo} alt="TraceForge" className={styles.heroLogo} />
        <Heading as="h1" className={styles.heroTitle}>
          <span className="text-gradient">TraceForge</span>
        </Heading>
        <p className={styles.heroTagline}>Observe. Understand. Control.</p>
        <p className={styles.heroPitch}>
          A framework-agnostic, CPU-only Python library that forges raw AI-agent traces into
          structured, classified, risk-scored, and governance-assessed output.
        </p>
        <div className={styles.heroButtons}>
          <Link className="button button--primary button--lg" to="/docs/intro">
            Get Started
          </Link>
          <Link
            className="button button--secondary button--outline button--lg"
            to="/docs/getting-started/installation">
            Quickstart
          </Link>
          <Link
            className="button button--secondary button--outline button--lg"
            href="https://github.com/dfinson/traceforge">
            GitHub
          </Link>
        </div>
        <div className={styles.pipeline} aria-label="Pipeline: Source, Parser, Adapter, Enricher, Pipeline, Sinks">
          {STAGES.map((stage, i) => (
            <span className={styles.pipelineRow} key={stage}>
              <span className={styles.stage}>{stage}</span>
              {i < STAGES.length - 1 && <span className={styles.arrow}>→</span>}
            </span>
          ))}
        </div>
      </div>
    </header>
  );
}

function Features() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className={styles.featureGrid}>
          {FEATURES.map((f) => (
            <div className={styles.featureCard} key={f.title}>
              <Heading as="h3" className={styles.featureTitle}>
                {f.title}
              </Heading>
              <p className={styles.featureBody}>{f.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function Home() {
  return (
    <Layout
      title="Observe. Understand. Control."
      description="Framework-agnostic, CPU-only Python pipeline that forges AI-agent traces into structured, classified, risk-scored, and governance-assessed output.">
      <Hero />
      <main>
        <Features />
      </main>
    </Layout>
  );
}
