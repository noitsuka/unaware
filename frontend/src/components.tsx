import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { id } from './team';

let indexes: Record<string, number> = {};
fetch('/assets/pokemon-icons.json').then(response => response.json()).then(data => { indexes = data; window.dispatchEvent(new Event('pokemon-icons-loaded')); }).catch(() => {});

export function PokemonIcon({ species, small = false }: { species: string; small?: boolean }) {
  const element = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const update = () => {
      const number = indexes[id(species)];
      const node = element.current;
      if (!node) return;
      node.style.backgroundPosition = number ? `-${(number % 12) * 40}px -${Math.floor(number / 12) * 30}px` : '0px 0px';
    };
    update();
    window.addEventListener('pokemon-icons-loaded', update);
    return () => window.removeEventListener('pokemon-icons-loaded', update);
  }, [species]);
  return <span ref={element} className={'pokemon-icon' + (small ? ' small' : '')} aria-hidden="true" title={species} />;
}

export function IconButton({ label, children, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button type="button" aria-label={label} title={label} className={'icon-button ' + className} {...props}>{children}</button>;
}

export function AngleFrame({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={'angle-frame ' + className}><div className="angle-surface">{children}</div></div>;
}

export function Panel({ title, children, className = '' }: { title: string; children: ReactNode; className?: string }) {
  return <section className={'panel ' + className} aria-label={title}><AngleFrame><div className="panel-body">{children}</div></AngleFrame><h2 className="section-tag">{title}</h2></section>;
}

export function TierSelect({ options, value, onChange, disabled }: { options: Record<string, string>; value: string; onChange: (value: string) => void; disabled?: boolean }) {
  return <div className="select-wrap"><select aria-label="Battle format" value={value} onChange={event => onChange(event.target.value)} disabled={disabled}>
    {Object.keys(options).length ? Object.entries(options).map(([key, name]) => <option key={key} value={key}>{name.replace('[Gen 9]', 'Gen 9')}</option>) : <option value={value}>{value.replace(/^gen9/, 'Gen 9 ').toUpperCase()}</option>}
  </select><ChevronDown size={17} aria-hidden="true" /></div>;
}

export function Toggle({ checked, disabled, onChange, label }: { checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void; label: string }) {
  return <button type="button" className={'toggle ' + (checked ? 'on' : '')} role="switch" aria-label={label} aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)}><span /></button>;
}

export function Modal({ title, children, footer, onClose, className = '' }: { title: string; children: ReactNode; footer?: ReactNode; onClose: () => void; className?: string }) {
  const heading = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    const candidates = () => Array.from(node?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]') ?? []).filter(item => item.getClientRects().length);
    (node?.querySelector<HTMLElement>('input:not(:disabled),textarea:not(:disabled)') ?? candidates()[0])?.focus();
    const keyboard = (event: KeyboardEvent) => {
      const stack = document.querySelectorAll('[role="dialog"]');
      if (stack.item(stack.length - 1) !== node) return;
      if (event.key === 'Escape') { event.preventDefault(); close.current(); }
      if (event.key === 'Tab') {
        const items = candidates(), first = items[0], last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', keyboard);
    return () => { document.removeEventListener('keydown', keyboard); before?.focus(); };
  }, []);
  // Rendered at the shell level: inside a card, later siblings painted over it.
  return createPortal(<div className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={heading} className={'modal ' + className}>
    <header className="modal-header"><h2 id={heading}>{title}</h2><IconButton label="Close" onClick={onClose}><X size={20} /></IconButton></header>
    <div className="modal-content">{children}</div>{footer && <footer className="modal-footer">{footer}</footer>}
  </div></div>, document.querySelector('.desktop-shell') ?? document.body);
}

export function Field({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
  return <label className={'field ' + className}><span>{label}</span>{children}</label>;
}
