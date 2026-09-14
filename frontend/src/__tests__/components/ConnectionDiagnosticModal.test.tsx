/**
 * Tests for the connection diagnostic modal.
 *
 * Covers the user-facing contract: the modal runs the diagnostic on mount,
 * renders each check's localized title and fix text keyed on id + status,
 * picks the right API for printer vs pre-add mode, and re-runs on retry.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { ConnectionDiagnosticModal } from '../../components/ConnectionDiagnostic';
import { api, type PrinterDiagnosticResult } from '../../api/client';

const PROBLEM_RESULT: PrinterDiagnosticResult = {
  printer_id: 1,
  ip_address: '192.168.1.50',
  overall: 'problems',
  checks: [
    { id: 'port_mqtt', status: 'pass', params: {} },
    { id: 'port_ftps', status: 'pass', params: {} },
    { id: 'port_rtsps', status: 'warn', params: {} },
    { id: 'network_mode', status: 'pass', params: { mode: 'host' } },
    { id: 'subnet', status: 'pass', params: {} },
    { id: 'mqtt_auth', status: 'pass', params: {} },
    { id: 'developer_mode', status: 'fail', params: {} },
  ],
};

function renderModal(props: Parameters<typeof ConnectionDiagnosticModal>[0]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <I18nextProvider i18n={i18n}>
        <ConnectionDiagnosticModal {...props} />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('ConnectionDiagnosticModal', () => {
  it('runs the diagnostic on mount and renders check titles + the overall banner', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue(PROBLEM_RESULT);

    renderModal({ printerId: 1, printerName: 'Test P1S', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith(1);

    // Each check's localized title renders.
    expect(await screen.findByText(/Control port \(MQTT 8883\)/i)).toBeInTheDocument();
    expect(screen.getByText(/LAN Developer Mode/i)).toBeInTheDocument();

    // The failing developer_mode check shows its fix text.
    expect(screen.getByText(/Developer Mode is OFF/i)).toBeInTheDocument();

    // Overall banner reflects "problems".
    expect(
      screen.getByText(/Found problems that explain why the printer/i),
    ).toBeInTheDocument();

    spy.mockRestore();
  });

  it('uses the pre-add API when given a connection instead of a printerId', async () => {
    const spy = vi.spyOn(api, 'diagnoseConnection').mockResolvedValue({
      ...PROBLEM_RESULT,
      printer_id: null,
    });

    renderModal({
      connection: { ip_address: '192.168.1.99', serial_number: '01P', access_code: 'abc' },
      onClose: vi.fn(),
    });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith({
      ip_address: '192.168.1.99',
      serial_number: '01P',
      access_code: 'abc',
    });

    spy.mockRestore();
  });

  it('re-runs the diagnostic when the user clicks Run again', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue(PROBLEM_RESULT);

    renderModal({ printerId: 1, printerName: 'Test P1S', onClose: vi.fn() });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText(/Run again/i));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));

    spy.mockRestore();
  });

  it('renders model-specific camera port diagnostics', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      ...PROBLEM_RESULT,
      overall: 'warnings',
      checks: [
        { id: 'port_mqtt', status: 'pass', params: {} },
        { id: 'port_ftps', status: 'pass', params: {} },
        {
          id: 'port_rtsps',
          status: 'warn',
          params: { protocol: 'Chamber Image', port: 6000 },
        },
      ],
    });

    renderModal({ printerId: 1, printerName: 'Test A1 Mini', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Camera port \(Chamber Image 6000\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Port 6000 is unreachable/i)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('shows the unsupported-model explanation for the external_storage skip reason (#2524)', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      ...PROBLEM_RESULT,
      overall: 'warnings',
      checks: [
        { id: 'external_storage', status: 'skip', params: { reason: 'unsupported_model' } },
      ],
    });

    renderModal({ printerId: 1, printerName: 'Test P1S', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    // The reason-specific variant renders, NOT the generic "needs a live
    // MQTT connection" skip text.
    expect(await screen.findByText(/no way to turn the option on/i)).toBeInTheDocument();
    expect(screen.queryByText(/needs a live MQTT connection/i)).not.toBeInTheDocument();

    spy.mockRestore();
  });

  it('names a refused access code when the printer said so (#2698)', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      ...PROBLEM_RESULT,
      checks: [{ id: 'mqtt_auth', status: 'fail', params: { reason: 'auth_rejected' } }],
    });

    renderModal({ printerId: 1, printerName: 'Test A1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    // States the printer refused us, instead of the hedged "most likely wrong"
    // text used when all we know is that there's no session.
    expect(await screen.findByText(/refused Bambuddy's credentials/i)).toBeInTheDocument();
    expect(screen.queryByText(/most likely wrong/i)).not.toBeInTheDocument();

    spy.mockRestore();
  });

  it('hedges on the mqtt_auth failure when the printer gave no reason (#2698)', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      ...PROBLEM_RESULT,
      checks: [{ id: 'mqtt_auth', status: 'fail', params: {} }],
    });

    renderModal({ printerId: 1, printerName: 'Test A1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/most likely wrong/i)).toBeInTheDocument();
    expect(screen.queryByText(/refused Bambuddy's credentials/i)).not.toBeInTheDocument();

    spy.mockRestore();
  });

  it('renders the Snapmaker U1 checks with their own advice, not Bambu advice', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      printer_id: 2,
      ip_address: '192.168.1.9',
      // The Snapmaker run summarises as pass/fail rather than ok/problems.
      overall: 'fail',
      checks: [
        { id: 'moonraker', status: 'pass', params: {} },
        { id: 'snapmaker_identity', status: 'pass', params: { machine_type: 'U1' } },
        { id: 'klipper_ready', status: 'fail', params: { state: 'shutdown' } },
      ],
    });

    renderModal({ printerId: 2, printerName: 'Workshop U1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Printer API \(Moonraker\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Identified as a Snapmaker U1/i)).toBeInTheDocument();
    // The halted firmware gets the restart advice, with its reported state.
    expect(screen.getByText(/reports .shutdown. instead of ready/i)).toBeInTheDocument();
    expect(screen.getByText(/FIRMWARE_RESTART/)).toBeInTheDocument();

    // None of Bambu's vocabulary leaks into a U1 result.
    expect(screen.queryByText(/Developer Mode/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/access code/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/LAN Only/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/8883|990/)).not.toBeInTheDocument();

    // The pass/fail summary resolves to a real sentence, not a raw i18n key.
    expect(screen.getByText(/Found problems that explain why the printer/i)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('explains an unreachable Moonraker and skips the checks behind it', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      printer_id: 2,
      ip_address: '192.168.1.9',
      overall: 'fail',
      checks: [
        { id: 'moonraker', status: 'fail', params: { error: 'connection refused' } },
        { id: 'snapmaker_identity', status: 'skip', params: {} },
        { id: 'klipper_ready', status: 'skip', params: {} },
      ],
    });

    renderModal({ printerId: 2, printerName: 'Workshop U1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/No answer from the printer at this address/i)).toBeInTheDocument();
    expect(screen.getByText(/connection refused/i)).toBeInTheDocument();
    expect(screen.getAllByText(/the printer could not be reached/i)).toHaveLength(2);

    spy.mockRestore();
  });

  it('picks the transport-error variant when klipper_ready reports no state', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      printer_id: 2,
      ip_address: '192.168.1.9',
      overall: 'fail',
      checks: [
        { id: 'moonraker', status: 'pass', params: {} },
        // Community firmware: answers, but reports no product_info.
        { id: 'snapmaker_identity', status: 'warn', params: { machine_type: '' } },
        { id: 'klipper_ready', status: 'fail', params: { error: 'timed out' } },
      ],
    });

    renderModal({ printerId: 2, printerName: 'Workshop U1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/firmware state could not be read/i)).toBeInTheDocument();
    // The state-based sentence would have interpolated a blank here.
    expect(screen.queryByText(/instead of ready/i)).not.toBeInTheDocument();
    expect(screen.getByText(/does not identify itself as a Snapmaker/i)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('drops the machine type from the identity line when the printer reports none', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      printer_id: 2,
      ip_address: '192.168.1.9',
      overall: 'pass',
      checks: [
        { id: 'moonraker', status: 'pass', params: {} },
        { id: 'snapmaker_identity', status: 'pass', params: { machine_type: '' } },
        { id: 'klipper_ready', status: 'pass', params: { state: 'ready' } },
      ],
    });

    renderModal({ printerId: 2, printerName: 'Workshop U1', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Identified as Snapmaker firmware.')).toBeInTheDocument();
    // A passing Snapmaker run gets the healthy banner, not the failure one.
    expect(screen.getByText(/No problems found/i)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('falls back to the generic skip text when no reason is present', async () => {
    const spy = vi.spyOn(api, 'diagnosePrinter').mockResolvedValue({
      ...PROBLEM_RESULT,
      overall: 'warnings',
      checks: [{ id: 'external_storage', status: 'skip', params: {} }],
    });

    renderModal({ printerId: 1, printerName: 'Test X1C', onClose: vi.fn() });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/needs a live MQTT connection/i)).toBeInTheDocument();

    spy.mockRestore();
  });
});
