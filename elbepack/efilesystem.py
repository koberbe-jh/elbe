# ELBE - Debian Based Embedded Rootfilesystem Builder
# Copyright (C) 2013  Linutronix GmbH
#
# This file is part of ELBE.
#
# ELBE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ELBE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ELBE.  If not, see <http://www.gnu.org/licenses/>.
#

import os
import time
import shutil
import subprocess
import io
import stat

from glob import glob
from tempfile import mkdtemp

from elbepack.asciidoclog import CommandError
from elbepack.filesystem import Filesystem
from elbepack.version import elbe_version
from elbepack.hdimg import do_hdimg
from elbepack.fstab import fstabentry
from elbepack.licencexml import copyright_xml

def copy_filelist( src, filelist, dst ):
    for f in filelist:
        f = f.rstrip("\n")
        if src.isdir(f) and not src.islink(f):
            if not dst.isdir(f):
                dst.mkdir(f)
            st = src.stat(f)
            dst.chown(f, st.st_uid, st.st_gid)
        else:
            subprocess.call(["cp", "-a", "--reflink=auto", src.fname(f), dst.fname(f)])
    # update utime which will change after a file has been copied into
    # the directory
    for f in filelist:
        f = f.rstrip("\n")
        if src.isdir(f) and not src.islink(f):
            shutil.copystat(src.fname(f), dst.fname(f))


def extract_target( src, xml, dst, log, cache ):
    # create filelists describing the content of the target rfs
    if xml.tgt.has("tighten") or xml.tgt.has("diet"):
        pkglist = [ n.et.text for n in xml.node('target/pkg-list') if n.tag == 'pkg' ]
        arch = xml.text("project/buildimage/arch", key="arch")

        if xml.tgt.has("diet"):
            withdeps = []
            for p in pkglist:
                deps = cache.get_dependencies( p )
                withdeps += [d.name for d in deps]
                withdeps += [p]

            pkglist = list( set( withdeps ) )

        file_list = []
        for line in pkglist:
            file_list += src.cat_file("var/lib/dpkg/info/%s.list" %(line))
            file_list += src.cat_file("var/lib/dpkg/info/%s.conffiles" %(line))

            file_list += src.cat_file("var/lib/dpkg/info/%s:%s.list" %(line, arch))
            file_list += src.cat_file("var/lib/dpkg/info/%s:%s.conffiles" %(line, arch))

        file_list = list(sorted(set(file_list)))
        copy_filelist(src, file_list, dst)
    else:
        # first copy most diretories
        for f in src.listdir():
            subprocess.call(["cp", "-a", "--reflink=auto", f, dst.fname('')])

    try:
        dst.mkdir_p("dev")
    except:
        pass
    try:
        dst.mkdir_p("proc")
    except:
        pass
    try:
        dst.mkdir_p("sys")
    except:
        pass

    if xml.tgt.has("setsel"):
        pkglist = [ n.et.text for n in xml.node ('target/pkg-list') if n.tag == 'pkg' ]
        psel = 'var/cache/elbe/pkg-selections'

        with open (dst.fname (psel), 'w+') as f:
            for item in pkglist:
                f.write("%s  install\n" % item)

        log.chroot (dst, "dpkg --clear-selections")
        log.chroot (dst, "dpkg --set-selections < %s " % dst.fname (psel))
        log.chroot (dst, "dpkg --purge -a")


class ElbeFilesystem(Filesystem):
    def __init__(self, path, clean=False):
        Filesystem.__init__(self,path,clean)

    def dump_elbeversion(self, xml):
        f = self.open("etc/elbe_version", "w+")
        f.write("%s %s\n" %(xml.prj.text("name"), xml.prj.text("version")))
        f.write("this RFS was generated by elbe %s\n" % (elbe_version))
        f.write(time.strftime("%c\n"))
        f.close()

        version_file = self.open("etc/updated_version", "w")
        version_file.write( xml.text ("/project/version") )
        version_file.close

        elbe_base = self.open("etc/elbe_base.xml", "wb")
        xml.xml.write(elbe_base)
        self.chmod("etc/elbe_base.xml", stat.S_IREAD)

    def write_licenses(self, f, log, xml_fname=None):
        licence_xml = copyright_xml()
        for dir in self.listdir("usr/share/doc/", skiplinks=True):
            try:
                with io.open(os.path.join(dir, "copyright"), "rb") as lic:
                    lic_text = lic.read()
            except IOError as e:
                log.printo( "Error while processing license file %s: '%s'" %
                        (os.path.join(dir, "copyright"), e.strerror))
                lic_text = "Error while processing license file %s: '%s'" % (os.path.join(dir, "copyright"), e.strerror)

            try:
                lic_text = unicode (lic_text, encoding='utf-8')
            except:
                lic_text = unicode (lic_text, encoding='iso-8859-1')


            if not f is None:
                f.write(unicode(os.path.basename(dir)))
                f.write(u":\n================================================================================")
                f.write(u"\n")
                f.write(lic_text)
                f.write(u"\n\n")

            if not xml_fname is None:
                licence_xml.add_copyright_file (os.path.basename(dir), lic_text)

        if not xml_fname is None:
            licence_xml.write (xml_fname)


class ChRootFilesystem(ElbeFilesystem):
    def __init__(self, path, interpreter=None, clean=False):
        ElbeFilesystem.__init__(self,path,clean)
        self.interpreter = interpreter
        self.cwd = os.open ("/", os.O_RDONLY)
        self.inchroot = False

    def __delete__ (self):
        os.close (self.cwd)

    def __enter__(self):
        if self.interpreter:
            if not self.exists ("usr/bin"):
                self.mkdir ("usr/bin")

            ui = "/usr/share/elbe/qemu-elbe/" + self.interpreter
            if not os.path.exists (ui):
                ui = "/usr/bin/" + self.interpreter
            os.system ('cp %s %s' % (ui, self.fname( "usr/bin" )))

        if self.exists ("/etc/resolv.conf"):
            os.system ('mv %s %s' % (self.fname ("etc/resolv.conf"),
                                     self.fname ("etc/resolv.conf.orig")))
        os.system ('cp %s %s' % ("/etc/resolv.conf",
                                 self.fname("etc/resolv.conf")))

        if self.exists("/etc/apt/apt.conf"):
            os.system ('cp %s %s' % (self.fname ("/etc/apt/apt.conf"),
                                     self.fname ("/etc/apt/apt.conf.orig")))
        if os.path.exists ("/etc/apt/apt.conf"):
            os.system ('cp %s %s' % ("/etc/apt/apt.conf",
                                     self.fname("/etc/apt/")))

        self.mkdir_p ("usr/sbin")
        self.write_file ("usr/sbin/policy-rc.d",
            0755, "#!/bin/sh\nexit 101\n")

        self.mount()
        return self

    def __exit__(self, type, value, traceback):
        if self.inchroot:
            self.leave_chroot()
        self.umount()
        if self.interpreter:
            os.system( 'rm -f %s' %
                        os.path.join(self.path, "usr/bin/"+self.interpreter) )

        os.system ('rm -f %s' % (self.fname ("etc/resolv.conf")))

        if self.exists ("/etc/resolv.conf.orig"):
            os.system ('mv %s %s' % (self.fname ("etc/resolv.conf.orig"),
                                     self.fname ("etc/resolv.conf")))

        if self.exists("/etc/apt/apt.conf"):
            os.system ('rm -f %s' % (self.fname ("etc/apt/apt.conf")))

        if self.exists ("/etc/apt/apt.conf.orig"):
            os.system ('mv %s %s' % (self.fname ("etc/apt/apt.conf.orig"),
                                     self.fname ("etc/apt/apt.conf")))

        if self.exists("/usr/sbin/policy-rc.d"):
            os.system ('rm -f %s' % (self.fname ("usr/sbin/policy-rc.d")))

    def mount(self):
        if self.path == '/':
            return
        try:
            os.system ("mount -t proc none %s/proc" % self.path)
            os.system ("mount -t sysfs none %s/sys" % self.path)
            os.system ("mount -o bind /dev %s/dev" % self.path)
            os.system ("mount -o bind /dev/pts %s/dev/pts" % self.path)
        except:
            self.umount ()
            raise

    def enter_chroot (self):
        assert not self.inchroot

        os.environ["LANG"] = "C"
        os.environ["LANGUAGE"] = "C"
        os.environ["LC_ALL"] = "C"

        os.chdir(self.path)
        self.inchroot = True

        if self.path == '/':
            return

        os.chroot(self.path)


    def _umount (self, path):
        if os.path.ismount (path):
            os.system("umount %s" % path)

    def umount (self):
        if self.path == '/':
            return
        self._umount ("%s/proc/sys/fs/binfmt_misc" % self.path)
        self._umount ("%s/proc" % self.path)
        self._umount ("%s/sys" % self.path)
        self._umount ("%s/dev/pts" % self.path)
        self._umount ("%s/dev" % self.path)

    def leave_chroot (self):
        assert self.inchroot

        os.fchdir (self.cwd)

        self.inchroot = False
        if self.path == '/':
            return

        os.chroot (".")

class TargetFs(ChRootFilesystem):
    def __init__(self, path, log, xml, clean=True):
        ChRootFilesystem.__init__(self, path, xml.defs["userinterpr"], clean)
        self.log = log
        self.xml = xml
        self.images = []

    def write_fstab(self, xml):
        if not self.exists("etc"):
            self.mkdir("etc")

        f = self.open("etc/fstab", "w")
        if xml.tgt.has("fstab"):
            for fs in xml.tgt.node("fstab"):
                fstab = fstabentry(xml, fs)
                f.write (fstab.get_str ())
            f.close()

    def part_target(self, targetdir, grub_version):

        # create target images and copy the rfs into them
        self.images = do_hdimg( self.log, self.xml, targetdir, self, grub_version )

        if self.xml.has("target/package/tar"):
            targz_name = self.xml.text ("target/package/tar/name")
            try:
                options = ''
                if self.xml.has("target/package/tar/options"):
                    options = self.xml.text("target/package/tar/options")
                cmd = "tar cfz %(targetdir)s/%(targz_name)s -C %(sourcedir)s %(options)s ."
                args = dict(
                    options=options,
                    targetdir=targetdir,
                    targz_name=targz_name,
                    sourcedir=self.fname('')
                )
                self.log.do(cmd % args)
                # only append filename if creating tarball was successful
                self.images.append (targz_name)
            except CommandError as e:
                # error was logged; continue creating cpio image
                pass

        if self.xml.has("target/package/cpio"):
            oldwd = os.getcwd()
            cpio_name = self.xml.text("target/package/cpio/name")
            os.chdir(self.fname(''))
            try:
                self.log.do("find . -print | cpio -ov -H newc >%s" % os.path.join(targetdir,cpio_name) )
                # only append filename if creating cpio was successful
                self.images.append (cpio_name)
            except CommandError as e:
                # error was logged; continue
                pass

        if self.xml.has("target/package/squashfs"):
            oldwd = os.getcwd()
            sfs_name = self.xml.text("target/package/squashfs/name")
            os.chdir(self.fname(''))
            try:
                self.log.do("mksquashfs %s %s/%s -noappend -no-progress" % (self.fname(''), targetdir, sfs_name))
                # only append filename if creating mksquashfs was successful
                self.images.append (sfs_name)
            except CommandError as e:
                # error was logged; continue
                pass

class BuildImgFs(ChRootFilesystem):
    def __init__(self, path, interpreter):
        ChRootFilesystem.__init__(self, path, interpreter)
