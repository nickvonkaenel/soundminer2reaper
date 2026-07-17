"use client";

import {
  type ChangeEvent,
  type DragEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Phase = "idle" | "preparing" | "converting" | "done" | "error";

type FileBundle = {
  database: File | null;
  caches: File[];
  templates: File[];
  rejected: string[];
};

type ConversionSummary = {
  chainCount: number;
  skippedCount: number;
  report: string;
};

type WorkerMessage =
  | { type: "status"; message: string; progress: number }
  | {
      type: "result";
      zip: ArrayBuffer;
      summary: ConversionSummary;
    }
  | { type: "error"; message: string };

const EMPTY_BUNDLE: FileBundle = {
  database: null,
  caches: [],
  templates: [],
  rejected: [],
};

const DATABASE_PATTERN = /\.(sqlite|sqlite3|db)$/i;
const CACHE_PATTERN = /^reaper-vstplugins.*\.ini$/i;
const TEMPLATE_PATTERN = /^vst-.*\.ini$/i;

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function mergeUnique(existing: File[], incoming: File[]) {
  const merged = new Map<string, File>();
  for (const file of [...existing, ...incoming]) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    merged.set(key, file);
  }
  return [...merged.values()];
}

function FileCard({
  eyebrow,
  title,
  detail,
  count,
  ready,
  onClear,
}: {
  eyebrow: string;
  title: string;
  detail: string;
  count?: number;
  ready: boolean;
  onClear: () => void;
}) {
  return (
    <article className={`file-card ${ready ? "is-ready" : ""}`}>
      <div className="file-card-topline">
        <span className="file-eyebrow">{eyebrow}</span>
        <span className="file-state" aria-hidden="true">
          {ready ? "●" : "○"}
        </span>
      </div>
      <strong>{title}</strong>
      <p>{detail}</p>
      {ready && (
        <button type="button" className="text-button" onClick={onClear}>
          Remove{count && count > 1 ? ` ${count} files` : ""}
        </button>
      )}
    </article>
  );
}

export default function Home() {
  const inputRef = useRef<HTMLInputElement>(null);
  const workerRef = useRef<Worker | null>(null);
  const [bundle, setBundle] = useState<FileBundle>(EMPTY_BUNDLE);
  const [isDragging, setIsDragging] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState(
    "Add a Soundminer database to begin.",
  );
  const [progress, setProgress] = useState(0);
  const [summary, setSummary] = useState<ConversionSummary | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [downloadSize, setDownloadSize] = useState(0);

  const cacheSize = useMemo(
    () => bundle.caches.reduce((total, file) => total + file.size, 0),
    [bundle.caches],
  );
  const templateSize = useMemo(
    () => bundle.templates.reduce((total, file) => total + file.size, 0),
    [bundle.templates],
  );

  const revokeDownload = useCallback(() => {
    setDownloadUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return null;
    });
  }, []);

  useEffect(() => {
    return () => {
      workerRef.current?.terminate();
      if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    };
  }, [downloadUrl]);

  const addFiles = useCallback((incoming: File[]) => {
    if (!incoming.length) return;
    setBundle((current) => {
      let database = current.database;
      const caches: File[] = [];
      const templates: File[] = [];
      const rejected: string[] = [];

      for (const file of incoming) {
        if (DATABASE_PATTERN.test(file.name)) {
          database = file;
        } else if (CACHE_PATTERN.test(file.name)) {
          caches.push(file);
        } else if (TEMPLATE_PATTERN.test(file.name)) {
          templates.push(file);
        } else {
          rejected.push(file.name);
        }
      }

      return {
        database,
        caches: mergeUnique(current.caches, caches),
        templates: mergeUnique(current.templates, templates),
        rejected,
      };
    });
    setPhase("idle");
    setSummary(null);
    setProgress(0);
    setStatus("Files recognized. Review them, then start the conversion.");
    revokeDownload();
  }, [revokeDownload]);

  const onInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(event.dataTransfer.files));
  };

  const cancelConversion = useCallback(() => {
    workerRef.current?.terminate();
    workerRef.current = null;
    setPhase("idle");
    setProgress(0);
    setStatus("Conversion canceled. Your selected files are still here.");
  }, []);

  const reset = useCallback(() => {
    cancelConversion();
    setBundle(EMPTY_BUNDLE);
    setSummary(null);
    setStatus("Add a Soundminer database to begin.");
    revokeDownload();
  }, [cancelConversion, revokeDownload]);

  const startConversion = async () => {
    if (!bundle.database || phase === "preparing" || phase === "converting") {
      return;
    }

    workerRef.current?.terminate();
    revokeDownload();
    setSummary(null);
    setPhase("preparing");
    setProgress(5);
    setStatus("Reading your files…");

    try {
      const databaseBuffer = await bundle.database.arrayBuffer();
      const cacheFiles = await Promise.all(
        bundle.caches.map(async (file) => ({
          name: file.name,
          buffer: await file.arrayBuffer(),
        })),
      );
      const templateFiles = await Promise.all(
        bundle.templates.map(async (file) => ({
          name: file.name,
          buffer: await file.arrayBuffer(),
        })),
      );

      const workerUrl = new URL("converter-worker.mjs", document.baseURI);
      const worker = new Worker(workerUrl, { type: "module" });
      workerRef.current = worker;

      worker.onmessage = (event: MessageEvent<WorkerMessage>) => {
        const message = event.data;
        if (message.type === "status") {
          setPhase("converting");
          setStatus(message.message);
          setProgress(message.progress);
          return;
        }

        if (message.type === "result") {
          const blob = new Blob([message.zip], { type: "application/zip" });
          const url = URL.createObjectURL(blob);
          setDownloadUrl(url);
          setDownloadSize(blob.size);
          setSummary(message.summary);
          setPhase("done");
          setProgress(100);
          setStatus(
            message.summary.chainCount
              ? `${message.summary.chainCount} chain${
                  message.summary.chainCount === 1 ? "" : "s"
                } ready to download.`
              : "Conversion finished. Review the report for skipped presets.",
          );
          worker.terminate();
          workerRef.current = null;
          return;
        }

        setPhase("error");
        setStatus(message.message);
        setProgress(0);
        worker.terminate();
        workerRef.current = null;
      };

      worker.onerror = () => {
        setPhase("error");
        setStatus(
          "The browser converter could not start. Check your connection and try again.",
        );
        setProgress(0);
        worker.terminate();
        workerRef.current = null;
      };

      const transferables = [
        databaseBuffer,
        ...cacheFiles.map((file) => file.buffer),
        ...templateFiles.map((file) => file.buffer),
      ];
      worker.postMessage(
        {
          type: "convert",
          database: {
            name: bundle.database.name,
            buffer: databaseBuffer,
          },
          caches: cacheFiles,
          templates: templateFiles,
        },
        transferables,
      );
    } catch {
      setPhase("error");
      setProgress(0);
      setStatus("One of the selected files could not be read. Try selecting it again.");
    }
  };

  const isBusy = phase === "preparing" || phase === "converting";
  const databaseReady = Boolean(bundle.database);

  return (
    <main className="site-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="soundminer2reaper home">
          <span className="brand-mark" aria-hidden="true">
            S→R
          </span>
          <span>soundminer2reaper</span>
        </a>
        <div className="local-badge">
          <span className="pulse-dot" aria-hidden="true" />
          Runs locally in your browser
        </div>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="kicker">Soundminer presets → REAPER FX chains</p>
          <h1>
            Your plugin library,
            <br />
            ready to <em>drop in.</em>
          </h1>
          <p className="hero-lede">
            Convert Soundminer VST2 and VST3 presets into organized REAPER
            chains. No install, no account, and no file uploads.
          </p>
        </div>
        <div className="hero-note">
          <span className="hero-note-number">100%</span>
          <span>on-device processing</span>
          <p>Your database never leaves this tab.</p>
        </div>
      </section>

      <section className="converter" aria-labelledby="converter-title">
        <div className="converter-heading">
          <div>
            <p className="section-index">01 / Add your files</p>
            <h2 id="converter-title">Build a conversion bundle</h2>
          </div>
          <button
            type="button"
            className="text-button reset-button"
            onClick={reset}
            disabled={!databaseReady && !bundle.caches.length && !bundle.templates.length}
          >
            Clear all
          </button>
        </div>

        <div
          className={`drop-zone ${isDragging ? "is-dragging" : ""}`}
          role="button"
          tabIndex={0}
          onDragEnter={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (event.currentTarget === event.target) setIsDragging(false);
          }}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              inputRef.current?.click();
            }
          }}
        >
          <input
            ref={inputRef}
            className="visually-hidden"
            type="file"
            multiple
            accept=".sqlite,.sqlite3,.db,.ini"
            onChange={onInputChange}
          />
          <span className="drop-symbol" aria-hidden="true">
            +
          </span>
          <div>
            <strong>Drop conversion files here</strong>
            <p>
              or <span>choose files</span> from your computer
            </p>
          </div>
          <small>.SQLITE · REAPER-VSTPLUGINS*.INI · VST-*.INI</small>
        </div>

        <div className="file-grid">
          <FileCard
            eyebrow="Required"
            title={bundle.database?.name ?? "Soundminer database"}
            detail={
              bundle.database
                ? formatBytes(bundle.database.size)
                : "pluginpresets.sqlite or another .sqlite/.db file"
            }
            ready={Boolean(bundle.database)}
            onClear={() =>
              setBundle((current) => ({ ...current, database: null }))
            }
          />
          <FileCard
            eyebrow="For VST3"
            title={
              bundle.caches.length
                ? `${bundle.caches.length} REAPER scan cache${
                    bundle.caches.length === 1 ? "" : "s"
                  }`
                : "REAPER scan cache"
            }
            detail={
              bundle.caches.length
                ? formatBytes(cacheSize)
                : "reaper-vstplugins*.ini resolves plugin identities"
            }
            count={bundle.caches.length}
            ready={bundle.caches.length > 0}
            onClear={() =>
              setBundle((current) => ({ ...current, caches: [] }))
            }
          />
          <FileCard
            eyebrow="Optional VST2"
            title={
              bundle.templates.length
                ? `${bundle.templates.length} pin template${
                    bundle.templates.length === 1 ? "" : "s"
                  }`
                : "VST2 pin templates"
            }
            detail={
              bundle.templates.length
                ? formatBytes(templateSize)
                : "vst-*.ini files preserve exact channel routing"
            }
            count={bundle.templates.length}
            ready={bundle.templates.length > 0}
            onClear={() =>
              setBundle((current) => ({ ...current, templates: [] }))
            }
          />
        </div>

        {bundle.rejected.length > 0 && (
          <p className="inline-warning" role="alert">
            Not recognized: {bundle.rejected.slice(0, 4).join(", ")}
            {bundle.rejected.length > 4
              ? ` and ${bundle.rejected.length - 4} more`
              : ""}
          </p>
        )}

        {databaseReady && !bundle.caches.length && (
          <p className="inline-note">
            No REAPER scan cache selected. VST2 can still convert, but VST3
            entries without an identity match will be skipped.
          </p>
        )}

        <div className="conversion-controls">
          <div className="status-block" aria-live="polite">
            <div className="status-line">
              <span>{status}</span>
              {isBusy && <strong>{progress}%</strong>}
            </div>
            <div className="progress-track" aria-hidden="true">
              <span style={{ width: `${progress}%` }} />
            </div>
          </div>

          {isBusy ? (
            <button type="button" className="secondary-button" onClick={cancelConversion}>
              Cancel
            </button>
          ) : phase === "done" && downloadUrl ? (
            <a
              className="primary-button download-button"
              href={downloadUrl}
              download="soundminer-reaper-chains.zip"
            >
              Download ZIP
              <span>{formatBytes(downloadSize)}</span>
            </a>
          ) : (
            <button
              type="button"
              className="primary-button"
              disabled={!databaseReady}
              onClick={startConversion}
            >
              Make my chains
              <span aria-hidden="true">→</span>
            </button>
          )}
        </div>

        {summary && (
          <div className="result-panel">
            <div className="result-metric">
              <strong>{summary.chainCount}</strong>
              <span>chains created</span>
            </div>
            <div className="result-metric">
              <strong>{summary.skippedCount}</strong>
              <span>slots skipped</span>
            </div>
            <details>
              <summary>View conversion report</summary>
              <pre>{summary.report}</pre>
            </details>
          </div>
        )}
      </section>

      <section className="how-it-works" aria-labelledby="how-title">
        <div>
          <p className="section-index">02 / What happens</p>
          <h2 id="how-title">A local handoff, end to end.</h2>
        </div>
        <ol>
          <li>
            <span>1</span>
            <div>
              <strong>Read</strong>
              <p>The browser opens your Soundminer database in a private workspace.</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>Resolve</strong>
              <p>VST2 and VST3 state is matched to the REAPER metadata you provide.</p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>Package</strong>
              <p>Converted chains and a readable report are bundled into one ZIP.</p>
            </div>
          </li>
        </ol>
      </section>

      <footer>
        <p>
          Independent, open-source software. Soundminer and REAPER are trademarks
          of their respective owners.
        </p>
        <span>Nothing uploaded. Nothing stored.</span>
      </footer>
    </main>
  );
}
