"""Remote job orchestration tests; no SSH or hardware required."""
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import flash_esp_remote as remote


class RemoteJobChecks(unittest.TestCase):
    def test_detached_user_no_retry_and_local_log(self):
        staging = '/home/pi/mixos-flash-20260910-170000'
        worker = ['python3', '-u', staging + '/tools/flash_esp_on_pi.py', '--serial', 'TD0720']
        command = shlex.split(remote.detached_command(staging, 'pi', worker))
        self.assertEqual(command[:5], ['sudo', '-S', '-p', '', 'systemd-run'])
        self.assertEqual(command[command.index('--uid') + 1], 'pi')
        self.assertIn('--property=Restart=no', command)
        self.assertIn('--property=RuntimeMaxSec=1800', command)
        self.assertIn('--property=StandardOutput=append:' + staging + '/worker.log', command)
        self.assertEqual(command[command.index('--') + 1:], worker)
        self.assertNotIn('--scope', command)
        self.assertNotIn('--pty', command)

    def test_reject_unsafe_job_name(self):
        for name in ('other', 'mixos-flash-20260910-170000;reboot', '../etc/passwd'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                remote.show_job(Mock(), name)

    def test_status_never_starts_worker(self):
        with patch.object(remote, 'run', return_value=0) as run:
            remote.show_job(Mock(), 'mixos-flash-20260910-170000')
        command = run.call_args.args[1]
        self.assertIn('systemctl show', command)
        self.assertIn('flash-audit.jsonl', command)
        self.assertNotIn('systemd-run', command)
        self.assertNotIn('flash_esp_on_pi.py', command)

    def test_stdin_sent_without_password_in_command(self):
        client = Mock()
        channel = client.get_transport.return_value.open_session.return_value
        channel.recv_ready.return_value = False
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        self.assertEqual(remote.run(client, "sudo -S -p '' true", input_data=b'secret\n'), 0)
        channel.exec_command.assert_called_once_with("sudo -S -p '' true")
        channel.sendall.assert_called_once_with(b'secret\n')
        channel.shutdown_write.assert_called_once()
        channel.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
