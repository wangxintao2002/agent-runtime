import subprocess
import tempfile
import unittest
from pathlib import Path

from replay_git_objects import restore_object_storage,storage_manifest


class ObjectStorageTests(unittest.TestCase):
    def command(self,*args):
        return subprocess.run(['git',*map(str,args)],check=True,capture_output=True,text=True)

    def prepare(self,root):
        base=root/'base';self.command('init','-q',base)
        self.command('-C',base,'config','user.name','Fixture')
        self.command('-C',base,'config','user.email','fixture@localhost')
        (base/'code.py').write_text('base implementation\n')
        self.command('-C',base,'add','code.py');self.command('-C',base,'commit','-qm','base')
        recorded=root/'recorded';fresh=root/'fresh'
        for path in [recorded,fresh]:self.command('clone','-q','--no-hardlinks',base,path)
        self.command('-C',recorded,'repack','-ad')
        (recorded/'code.py').write_text('recorded solution must not leak\n')
        return base,recorded,fresh

    def test_restores_pack_bytes_but_never_recorded_solution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base,recorded,fresh=self.prepare(Path(tmp))
            before=storage_manifest(fresh/'.git/objects')
            self.assertNotEqual(before,storage_manifest(recorded/'.git/objects'))
            result=restore_object_storage(recorded,base,fresh)
            self.assertEqual(storage_manifest(fresh/'.git/objects'),storage_manifest(recorded/'.git/objects'))
            self.assertEqual((fresh/'code.py').read_text(),'base implementation\n')
            self.assertFalse(result['working_tree_copied'])
            self.assertNotEqual((fresh/'.git/objects/pack').stat().st_ino,(recorded/'.git/objects/pack').stat().st_ino)

    def test_extra_recorded_objects_are_rejected_before_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            base,recorded,fresh=self.prepare(Path(tmp))
            before=storage_manifest(fresh/'.git/objects')
            subprocess.run(['git','-C',str(recorded),'hash-object','-w','--stdin'],input='extra solution object',text=True,check=True,capture_output=True)
            with self.assertRaisesRegex(ValueError,'logical object set'):
                restore_object_storage(recorded,base,fresh)
            self.assertEqual(storage_manifest(fresh/'.git/objects'),before)


if __name__=='__main__':unittest.main()
