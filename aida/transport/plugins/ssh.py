from stat import S_ISDIR,S_ISREG
import StringIO
import paramiko
import os

import aida.transport
from aida.common.utils import escape_for_bash
from aida.common import aidalogger
from aida.common.extendeddicts import FixedFieldsAttributeDict

class FileAttribute(FixedFieldsAttributeDict):
    """
    A class with attributes of a file, that is returned by get_attribute()
    """
    _valid_fields = (
        'st_size',
        'st_uid',
        'st_gid',
        'st_mode',
        'st_atime',
        'st_mtime',
        )

class SshTransport(aida.transport.Transport):
    # Valid keywords accepted by the connect method of paramiko.SSHClient
    _valid_connect_params = ['port', 'username', 'password', 'pkey',
                             'key_filename', 'timeout', 'allow_agent',
                             'look_for_keys', 'compress']
    
    def __init__(self, machine, **kwargs):
        """
        Initialize the SshTransport class. 
        
        Args:
            machine: the machine to connect to
            load_system_host_keys (optional, default False): if False, do not
                load the system host keys
            key_policy (optional, default = paramiko.RejectPolicy()): the
                policy to use for unknown keys
            Other parameters valid for the ssh connect function (see the 
            self._valid_connect_params list) are passed to the connect
            function (as port, username, password, ...); taken from the
            accepted paramiko.SSHClient.connect() params)

            TODO : implement a property self.sftp that raises a reasonable
               exception if the channel has not been opened. Understand if
               we need explicit open() and close() functions.
        """

        ## First call the parent __init__ to setup the logger!
        super(SshTransport,self).__init__()
        self._logger = super(SshTransport,self).logger.getChild('ssh')

        self._is_open = False
        self._sftp = None
        
        self._machine = machine

        self._client = paramiko.SSHClient()
        self._load_system_host_keys = kwargs.pop(
            'load_system_host_keys', False)
        if self._load_system_host_keys:
            self._client.load_system_host_keys()

        self._missing_key_policy = kwargs.pop(
            'key_policy',paramiko.RejectPolicy()) # This is paramiko default
        self._client.set_missing_host_key_policy(self._missing_key_policy)

        self._connect_args = {}
        for k in self._valid_connect_params:
            try:
                self._connect_args[k] = kwargs.pop(k)
            except KeyError:
                pass

        if kwargs:
            raise ValueError("The following parameters were not accepted by "
                             "the transport: {}".format(
                    ",".join(str(k) for k in kwargs)))

    def __enter__(self):
        """
        Open a SSHClient to the machine possibly using the parameters given
        in the __init__. 
        
        Also opens a sftp channel, ready to be used.
        The current working directory is set explicitly, so it is not None.
        """
        
        # Open a SSHClient
        self._client.connect(self._machine,**self._connect_args)

        # Open also a SFTPClient
        self._sftp = self._client.open_sftp()
        # Set the current directory to a explicit path, and not to None
        self._sftp.chdir(self._sftp.normalize('.'))

        self._is_open = True
        
        return self
        
    def __exit__(self, type, value, traceback):
        """
        Close the SFTP channel, and the SSHClient.
        
        TODO : correctly manage exceptions
        """
        self._sftp.close()
        self._client.close()
        self._is_open = False

    @property
    def sshclient(self):
        if not self._is_open:
            raise aida.transport.TransportInternalError(
                "Error, ssh method called for SshTransport "
                "without opening the channel first")
        return self._client

    @property
    def sftp(self):
        if not self._is_open:
            raise aida.transport.TransportInternalError(
                "Error, sftp method called for SshTransport "
                "without opening the channel first")
        return self._sftp

    def __unicode__(self):
        """
        Return a useful string.
        """
        conn_info = unicode(self._machine)
        try:
            conn_info = (unicode(self._connect_args['username']) + 
                         u'@' + conn_info)
        except KeyError:
            # No username explicitly defined: ignore
            pass
        try:
            conn_info += u':{}'.format(self._connect_args['port'])
        except KeyError:
        # No port explicitly defined: ignore
            pass
            
        return u'{}({})'.format(self.__class__.__name__, conn_info)

    def chdir(self, path):
        """
        Change directory of the SFTP session. Emulated internally by
        paramiko. 

        Differently from paramiko, if you pass None to chdir, nothing
        happens and the cwd is unchanged.
        """
        if path is not None:
            self.sftp.chdir(path)

        # TODO : when chdir fails, it leaves the cwd in the directory it failed to open

        # Paramiko already checked that path is a folder, otherwise I would
        # have gotten an exception. Now, I want to check that I have read
        # permissions in this folder (nothing is said on write permissions,
        # though).
        # Otherwise, if I do _exec_command_internal, that as a first operation 
        # cd's in a folder, I get a wrong retval, that is an unwanted behavior.
        #
        # Note: I don't store the result of the function; if I have no
        # read permissions, this will raise an exception.
        self.sftp.stat('.')

    def normalize(self, path):
        return self.sftp.normalize(path)
        
    def getcwd(self):
        """
        Return the current working directory for this SFTP session, as
        emulated by paramiko. If no directory has been set with chdir,
        this method will return None. But in __enter__ this is set explicitly,
        so this should never happen within this class.
        """
        return self.sftp.getcwd()
    
    def mkdir(self, path):
        """
        Create a folder (directory) named path with numeric mode mode.

        Args:
            path: the folder to create. Relative paths refer to the
                current working directory.

        Raises:
            If the directory already exists, OSError is raised.
        """
        try:
            self.sftp.mkdir(path)
        except IOError as e:
            raise OSError(e.message)

    def rmdir(self, path):
        """
        Remove the folder named 'path'.
        """
        self.sftp.rmdir(path)

    def isdir(self,path):
        """
        Return True if the given path is a directory, False otherwise.
        Return False also if the path does not exist.
        """
        # Return False on empty string (paramiko would map this to the local
        # folder instead)
        if not path:
            return False
        try:
            return S_ISDIR(self.sftp.stat(path).st_mode)
        except IOError as e:
            if getattr(e,"errno",None) == 2:
                # errno=2 means path does not exist: I return False
                return False
            else:
                raise # Typically if I don't have permissions (errno=13)


    def chmod(self,path,mode):
        if not path:
            raise IOError("Input path is an empty argument.")
        return self.sftp.chmod(path,mode)
            

    def put(self,localpath,remotepath,callback=None):
        """
        Put a file from local to remote.

        Args:
            localpath: an (absolute) local path
            remotepath: a remote path
        
        Raises:
            OSError if the localpath does not exist
        """
        # TODO: flag confirm exists since v1.7.7. What is the paramiko
        # version supported?
        if not os.path.isabs(localpath):
            raise ValueError("The localpath must be an absolute path")
        return self.sftp.put(localpath,remotepath,callback=callback)


    def get(self,remotepath,localpath,callback=None):
        """
        get a file from remote to local
        """
        if not os.path.isabs(localpath):
            raise ValueError("The localpath must be an absolute path")
        return self.sftp.get(remotepath,localpath,callback)


    def get_attribute(self,path):
        """
        Returns the list of attributes of a file
        Receives in input the path of a given file
        """
        paramiko_attr = self.sftp.lstat(path)
        aida_attr = FileAttribute()
        # map the paramiko class into the aida one
        # note that paramiko object contains more informations than the aida
        for key in aida_attr._valid_fields:
            aida_attr[key] = getattr(paramiko_attr,key)
        return aida_attr
    

    def copy(self,remotesource,remotedestination,dereference=False):
        """
        Copy a file or a directory from remote source to remote destination.
        Flags used: -r: recursive copy; -f: force, makes the command non interactive;
          -L follows symbolic links

        Args:
            remotesource: file to copy from
            remotedestination: file to copy to
            dereference: if True, copy content instead of copying the symlinks only

        Raises:
            IOError if the cp execution failed.
        """
        # In the majority of cases, we should deal with linux cp commands
        
        # TODO : do we need to avoid the aliases when calling cp_exe='cp'? Call directly /bin/cp?
        
        # TODO: verify that it does not re

        # For the moment, these are hardcoded. They may become parameters
        # as soon as we see the need.
        
        cp_exe='cp'

        ## To evaluate if we also want -p: preserves mode,ownership and timestamp
        cp_flags='-r -f'
        if dereference:
            # use -L; --dereference is not supported on mac
            cp_flags+=' -L'

        # if in input I give an invalid object raise ValueError
        if not remotesource:
            raise ValueError('Input to copy() must be a non empty string. ' +
                             'Found instead %s as remotesource' % remotesource)

        if not remotedestination:
            raise ValueError('Input to copy() must be a non empty string. ' +
                             'Found instead %s as remotedestination' % remotedestination)
        
        command = '{} {} {} {}'.format(cp_exe,
                                       cp_flags,
                                       escape_for_bash(remotesource),
                                       escape_for_bash(remotedestination))

        retval,stdout,stderr = self.exec_command_wait(command)
        
        # TODO : check and fix below
        
        if retval == 0:
            if stderr.strip():
                self.logger.warning("There was nonempty stderr in the cp "
                                    "command: {}".format(stderr))
            return True
        else:
            self.logger.error("Problem executing cp. Exit code: {}, stdout: '{}', "
                              "stderr: '{}'".format(retval, stdout, stderr))
            raise IOError("Error while executing cp. Exit code: {}".format(retval) )

    def listdir(self,path='.'):
        return self.sftp.listdir(path)

    def remove(self,path):
        return self.sftp.remove(path)
    
    def isfile(self,path):
        """
        Return True if the given path is a file, False otherwise.
        Return False also if the path does not exist.
        """
        # This should not be needed for files, since an empty string should
        # be mapped by paramiko to the local directory - which is not a file -
        # but this is just to be sure
        if not path:
            return False
        try:
            self.logger.debug("stat for path '{}' ('{}'): {} [{}]".format(
                    path, self.sftp.normalize(path),
                    self.sftp.stat(path),self.sftp.stat(path).st_mode))
            return S_ISREG(self.sftp.stat(path).st_mode)
        except IOError as e:
            if getattr(e,"errno",None) == 2:
                # errno=2 means path does not exist: I return False
                return False
            else:
                raise # Typically if I don't have permissions (errno=13)

    def _exec_command_internal(self,command,combine_stderr=False,bufsize=-1):
        """
        Executes the specified command, first changing directory to the
        current working directory are returned by
        self.getcwd().
        Does not wait for the calculation to finish.

        For a higher-level _exec_command_internal that automatically waits for the
        job to finish, use exec_command_wait.

        Args:
            command: the command to execute
            combine_stderr (default False): if True, combine stdout and
                stderr on the same buffer (i.e., stdout).
                Note: If combine_stderr is True, stderr will always be empty.
            bufsize: same meaning of the one used by paramiko.
        
        Returns:
            a tuple with (stdin, stdout, stderr, channel),
            where stdin, stdout and stderr behave as file-like objects,
            plus the methods provided by paramiko, and channel is a 
            paramiko.Channel object.
        """
        channel = self.sshclient.get_transport().open_session()
        channel.set_combine_stderr(combine_stderr)

        if self.getcwd() is not None:
            escaped_folder = escape_for_bash(self.getcwd())
            command_to_execute = ("cd {escaped_folder} && "
                                  "{real_command}".format(
                    escaped_folder = escaped_folder,
                    real_command = command))
        else:
            command_to_execute = command

        self.logger.debug("Command to be executed: {}".format(
                command_to_execute))

        channel.exec_command(command_to_execute)
        
        stdin = channel.makefile('wb',bufsize) 
        stdout = channel.makefile('rb',bufsize) 
        stderr = channel.makefile_stderr('rb',bufsize) 

        return stdin, stdout, stderr, channel

    def exec_command_wait(self,command,stdin=None,combine_stderr=False,
                          bufsize=-1):
        """
        Executes the specified command and waits for it to finish.
        
        TODO: To see if like this it works or hangs because of buffer problems.

        Args:
            command: the command to execute
            stdin: can either be None (the default), a string or a
                   file-like object.
            combine_stderr: (optional, default=False) see docstring of
                   self._exec_command_internal()
            bufsize: same meaning of paramiko.

        Returns:
            a tuple with (return_value, stdout, stderr) where stdout and stderr
            are strings.
        """
        ssh_stdin, stdout, stderr, channel = self._exec_command_internal(
            command, combine_stderr,bufsize=bufsize)
        
        if stdin is not None:
            if isinstance(stdin, basestring):
                filelike_stdin = StringIO.StringIO(stdin)
            else:
                filelike_stdin = stdin

            try:
                for l in filelike_stdin.readlines():
                    ssh_stdin.write(l)
            except AttributeError:
                raise ValueError("stdin can only be either a string of a "
                                 "file-like object!")

        # I flush and close them anyway; important to call shutdown_write
        # to avoid hangouts
        ssh_stdin.flush()
        ssh_stdin.channel.shutdown_write()

        output_text = stdout.read()
        
        # I get the return code (blocking, but in principle I waited above)
        retval = channel.recv_exit_status()

        stderr_text = stderr.read()

        return retval, output_text, stderr_text




if __name__ == '__main__':
    import unittest
    import logging
    from test import *
#    aidalogger.setLevel(logging.DEBUG)


    class TestBasicConnection(unittest.TestCase):
        """
        Test basic connections.
        """

        def test_closed_connection_ssh(self):
            with self.assertRaises(aida.transport.TransportInternalError):
                t = SshTransport(machine='localhost')
                t._exec_command_internal('ls')

        def test_closed_connection_sftp(self):
            with self.assertRaises(aida.transport.TransportInternalError):
                t = SshTransport(machine='localhost')
                t.listdir()
                
        def test_invalid_param(self):
            with self.assertRaises(ValueError):
                SshTransport(machine='localhost', invalid_param=True)
                             

        def test_auto_add_policy(self):
            with SshTransport(machine='localhost', timeout=30, 
                              load_system_host_keys=True,
                              key_policy=paramiko.AutoAddPolicy()) as t:
                pass

        def test_no_host_key(self):
            with self.assertRaises(paramiko.SSHException):
                with SshTransport(machine='localhost', timeout=30, 
                                  load_system_host_keys = False) as t:
                    pass

    # Load the plugin tests
    run_tests('ssh')
    
    unittest.main()
