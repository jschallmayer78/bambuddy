/**
 * Tests for the printer-type gating on the printer card.
 *
 * Most of the card is Bambu's protocol surface: a Snapmaker U1's driver has no
 * equivalent for HMS faults, MQTT or AMS management, so those routes answer
 * HTTP 501. The card hides the controls rather than offering buttons that fail
 * on click. What both machines can do — temperatures, progress, the filament
 * slots — stays.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const basePrinter = {
  id: 1,
  ip_address: '192.168.1.100',
  access_code: '',
  enabled: true,
  is_active: true,
  location: null,
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const bambuPrinter = {
  ...basePrinter,
  name: 'X1 Carbon',
  serial_number: '00M09A350100001',
  printer_type: 'bambu',
  model: 'X1C',
};

const u1Printer = {
  ...basePrinter,
  name: 'Workshop U1',
  serial_number: 'u1-192-168-1-9',
  printer_type: 'snapmaker_u1',
  model: 'U1',
};

const baseStatus = {
  connected: true,
  state: 'IDLE',
  awaiting_plate_clear: false,
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 210, bed: 60 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  hms_errors: [],
  vt_tray: [],
  // The U1's four toolheads arrive as a synthetic AMS unit (unit 0, trays 0-3).
  ams: [
    {
      id: 0,
      humidity: null,
      temp: null,
      is_ams_ht: false,
      module_type: '',
      serial_number: '',
      sw_ver: '',
      dry_time: 0,
      dry_status: 0,
      dry_sub_status: 0,
      dry_sf_reason: [],
      tray: [0, 1, 2, 3].map((id) => ({
        id,
        tray_color: 'FF6A13FF',
        tray_type: 'PLA',
        tray_sub_brands: 'PLA Basic',
        tray_id_name: null,
        tray_info_idx: null,
        remain: 80,
        k: null,
        cali_idx: null,
        tag_uid: null,
        tray_uuid: null,
        nozzle_temp_min: 190,
        nozzle_temp_max: 240,
        state: 11,
        exists: true,
      })),
    },
  ],
  ams_exists: true,
  tray_now: 0,
};

function mockWith(printers: unknown[]) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(baseStatus)),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

describe('PrinterCard printer-type gating', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
  });

  it('hides the Bambu-only controls on a Snapmaker U1 card', async () => {
    mockWith([u1Printer]);
    render(<PrintersPage />);

    // The synthetic unit is the four toolheads, not an AMS. Waiting on it also
    // waits for the status to arrive, so the absences below are real.
    expect(await screen.findByText('Toolheads')).toBeInTheDocument();
    expect(screen.queryByText('AMS-A')).not.toBeInTheDocument();
    // HMS is Bambu's fault-code system; the badge would open an empty modal.
    expect(screen.queryByTitle(/view hms errors/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByTitle(/more$/i));
    expect(screen.queryByText(/mqtt debug/i)).not.toBeInTheDocument();
    // The menu itself still works — this is a gate, not a broken render.
    expect(screen.getByText(/force refresh/i)).toBeInTheDocument();
  });

  it('keeps the shared controls on a Snapmaker U1 card', async () => {
    mockWith([u1Printer]);
    render(<PrintersPage />);

    // Temperatures and the filament slots are the same on both protocols.
    expect(await screen.findByText('210°C')).toBeInTheDocument();
    expect(screen.getByText('60°C')).toBeInTheDocument();
    expect(screen.getAllByText('PLA').length).toBeGreaterThan(0);
  });

  it('still offers the Bambu-only controls on a Bambu card', async () => {
    mockWith([bambuPrinter]);
    render(<PrintersPage />);

    expect(await screen.findByText('AMS-A')).toBeInTheDocument();
    expect(screen.getByTitle(/view hms errors/i)).toBeInTheDocument();

    await userEvent.click(screen.getByTitle(/more$/i));
    expect(screen.getByText(/mqtt debug/i)).toBeInTheDocument();
  });
});
