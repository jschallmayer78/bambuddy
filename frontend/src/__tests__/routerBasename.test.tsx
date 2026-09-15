/**
 * The router's basename, which is what keeps in-app navigation inside Home
 * Assistant's ingress prefix.
 *
 * Route paths and navigate() targets throughout the app are written as if the
 * app were mounted at "/". The basename is the single place that translates
 * them, so this checks both directions: a link renders *with* the prefix, and a
 * location that already carries the prefix still matches the bare route path.
 */

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Link, useNavigate } from 'react-router-dom';

const INGRESS = '/api/hassio_ingress/aBc123XyZ/';

function Printers() {
  const navigate = useNavigate();
  return (
    <div>
      <span>printers page</span>
      <Link to="/queue">to queue</Link>
      <button onClick={() => navigate('/queue')}>go</button>
    </div>
  );
}

function App({ basename, initial }: { basename: string; initial: string }) {
  return (
    <MemoryRouter basename={basename} initialEntries={[initial]}>
      <Routes>
        <Route path="/" element={<Printers />} />
        <Route path="/queue" element={<span>queue page</span>} />
      </Routes>
    </MemoryRouter>
  );
}

afterEach(cleanup);

describe('router basename', () => {
  it('matches a bare route path under an ingress prefix', () => {
    render(<App basename={INGRESS} initial={`${INGRESS}queue`} />);
    expect(screen.getByText('queue page')).toBeInTheDocument();
  });

  it('renders links with the prefix applied', () => {
    render(<App basename={INGRESS} initial={INGRESS} />);
    expect(screen.getByText('to queue').getAttribute('href')).toBe(`${INGRESS}queue`);
  });

  it('lands a navigate("/queue") on the right route under a prefix', () => {
    render(<App basename={INGRESS} initial={INGRESS} />);
    expect(screen.getByText('printers page')).toBeInTheDocument();
    fireEvent.click(screen.getByText('go'));
    expect(screen.getByText('queue page')).toBeInTheDocument();
  });

  it('is a no-op on direct access', () => {
    render(<App basename="/" initial="/queue" />);
    expect(screen.getByText('queue page')).toBeInTheDocument();
    cleanup();
    render(<App basename="/" initial="/" />);
    expect(screen.getByText('to queue').getAttribute('href')).toBe('/queue');
  });
});
