/**
 * Tests for the Add-Printer setup-time pre-flight.
 *
 * On save, the modal runs the connection diagnostic; if any check fails it
 * warns (rather than blocks) before the printer is added.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

describe('AddPrinterModal pre-flight', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/discovery/info', () =>
        HttpResponse.json({ is_docker: false, ssdp_running: false, scan_running: false, subnets: [] }),
      ),
    );
  });

  it('warns instead of saving when a connection check fails', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.55',
          overall: 'problems',
          checks: [{ id: 'developer_mode', status: 'fail', params: {} }],
        }),
      ),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));

    await user.type(await screen.findByPlaceholderText('My Printer'), 'Test Printer');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.55');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), '01P00A000000000');
    await user.type(screen.getByPlaceholderText('From printer settings'), '12345678');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    // The failed check surfaces a warning with a "save anyway" escape hatch.
    expect(await screen.findByText(/Some connection checks failed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save anyway/i })).toBeInTheDocument();
    expect(screen.getByText(/LAN Developer Mode/i)).toBeInTheDocument();
  });

  it('saves directly when all connection checks pass', async () => {
    const user = userEvent.setup();
    let created = false;
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.55',
          overall: 'ok',
          checks: [{ id: 'developer_mode', status: 'pass', params: {} }],
        }),
      ),
      http.post('/api/v1/printers/', async () => {
        created = true;
        return HttpResponse.json({ id: 9, name: 'Test Printer' });
      }),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));

    await user.type(await screen.findByPlaceholderText('My Printer'), 'Test Printer');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.55');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), '01P00A000000000');
    await user.type(screen.getByPlaceholderText('From printer settings'), '12345678');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    await waitFor(() => expect(created).toBe(true));
    expect(screen.queryByText(/Some connection checks failed/i)).not.toBeInTheDocument();
  });

  it('uses the plain connection test for a Snapmaker U1 and derives its serial', async () => {
    const user = userEvent.setup();
    let diagnosticCalls = 0;
    let probedType: string | null = null;
    let created: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/printers/diagnostic', () => {
        diagnosticCalls += 1;
        return HttpResponse.json({ printer_id: null, ip_address: '', overall: 'ok', checks: [] });
      }),
      http.post('/api/v1/printers/test', ({ request }) => {
        probedType = new URL(request.url).searchParams.get('printer_type');
        return HttpResponse.json({ success: true, model: 'U1' });
      }),
      http.post('/api/v1/printers/', async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 10, name: 'Shop U1' });
      }),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));
    await user.selectOptions(await screen.findByLabelText(/printer type/i), 'snapmaker_u1');

    // Neither serial nor access code is filled in: a U1 has neither.
    await user.type(await screen.findByPlaceholderText('My Printer'), 'Shop U1');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.9');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    await waitFor(() => expect(created).not.toBeNull());
    expect(created!.printer_type).toBe('snapmaker_u1');
    // The backend requires a unique non-empty serial; a blank field derives one.
    expect(created!.serial_number).toBe('u1-192-168-1-9');
    expect(probedType).toBe('snapmaker_u1');
    // The Bambu port diagnostic would warn about ports a U1 never opens.
    expect(diagnosticCalls).toBe(0);
  });

  it('warns with the probe reason when a Snapmaker U1 does not answer', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('/api/v1/printers/test', () =>
        HttpResponse.json({ success: false, reason: 'Moonraker did not respond' }),
      ),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));
    await user.selectOptions(await screen.findByLabelText(/printer type/i), 'snapmaker_u1');
    await user.type(await screen.findByPlaceholderText('My Printer'), 'Shop U1');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.9');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    expect(await screen.findByText('Moonraker did not respond')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save anyway/i })).toBeInTheDocument();
    // No Bambu checklist: there are no Bambu checks to show.
    expect(screen.queryByText(/LAN Developer Mode/i)).not.toBeInTheDocument();
  });
});
