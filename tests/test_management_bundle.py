"""Ensure the frozen Linux wizard carries its installer resource and own bundle."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main(executable):
    with tempfile.TemporaryDirectory(prefix='management bundle ') as directory:
        root=Path(directory)
        config=root/'worker.toml'
        config.write_text('worker_id="test"\nclient_id="pal"\nclient_public_key="'+ 'ab'*32 +
                          '"\nsocket_path="'+str(root/'worker.sock')+'"\n')
        output=root/'prepared'
        master,slave=os.openpty()
        process=None
        try:
            process=subprocess.Popen([str(Path(executable).resolve()),'--config',str(config),'--setup-sudo'],
                                     stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            os.close(slave); slave=None
            os.write(master,('1\n\n\n\n'+str(output)+'\n').encode())
            log,_=process.communicate(timeout=60)
            assert process.returncode==0,log.decode()
            setup=next(output.glob('setup-*'))
            assert (setup/'install-root.py').is_file(),log.decode()
            assert (setup/'bundle/_internal').is_dir()
            assert (setup/'INSTALL_MANIFEST.json').is_file()
            assert b'sudo /usr/bin/python3 -I' in log
            compile((setup/'install-root.py').read_text(), 'install-root.py','exec')
            print('Frozen setup generated complete bundle, manifest and standalone installer; no elevation or activation.')
        finally:
            if process and process.poll() is None:
                process.kill();process.wait()
            os.close(master)
            if slave is not None:os.close(slave)


if __name__=='__main__':
    main(sys.argv[1])
