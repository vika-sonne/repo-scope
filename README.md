# repo-master
**:snake: Pure Python repository command-line tool. Download/mirror and analyse .rpm based repositories.**
_____________________________________________________

## Install
```
pip3 install requests
```

## Using

### Usage scenario

- Repository Mirroring. Download and share repos. Be free to archive repos with partial mirroring using packages filters while downloading.
- Repository Analysing. Download with packages filters and explore packages relations, get files table, statistical info.

### Get help
```
./obs_repos -h
usage: obs_repos [-h] [-v] [--version] [-r PATH] [-p NAME] [-a NAME] [-x NAME] [-d] {download,d,architectures,a,filter,f,filelists,fl,primary,pr} ...

OBS (Open Build Service) repository tool: download/mirror and analyse .rpm based repositories.
Version 2025.5

Useful for repositories (at least but not at last):
  openSUSE http://download.opensuse.org/tumbleweed/repo/oss/
  Sailfish https://repo.sailfishos.org/obs/sailfishos:/

This pure python utility requires at least python 3.12.

positional arguments:
  {download,d,architectures,a,filter,f,filelists,fl,primary,pr}
                        subcommands
    download (d)        download repositories meta files from OBS to cache; to download .rpm files use -a/--arch option
    architectures (a)   show architectures data
    filter (f)          process repositories from cache by various filters (main usage)
    filelists (fl)      show filelists files data
    primary (pr)        show primary files data

options:
  -h, --help            show this help message and exit
  -v, --verbose         verbose level; example: -vvv; default: none
  --version             show program's version number and exit
  -r PATH, --repos-path PATH
                        repositories path; default: /home/vika/Documents/OMP/tests/Study/.repos_cache
  -p NAME, --package NAME
                        package filter by name, space separated values; = exactly, ~ part, ^ starts with, # ends with, ! not; examples: "timed", "^timed #!-doc", "!timed", "=!timed", "~!timed", "^!timed", "#!timed"
  -a NAME, --arch NAME  package filter by architecture, space separated values; example: "aarch64 armv7hl x86_64 noarch src"
  -x NAME, --exclude-arch NAME
                        package filter by architecture, space separated values; example: "aarch64 armv7hl x86_64"
  -d, --exclude-devel   package filter for test/debug/devel

Examples:

  Download repositories "apps system games" for architectures "armv7hl noarch" from OBS site to cache path ".repos_cache_15.6" with package download size-max filter:
obs_repos -r ./repos_cache_15.6 -a "armv7hl noarch" d -u 'https://example.org/{repo}:/15.6/' -e "apps system games" -s 1_000_000
  Will be downloaded 3 repositories by URLs:
  https://example.org/apps:/15.6/repodata/repomd.xml
  https://example.org/system:/15.6/repodata/repomd.xml
  https://example.org/games:/15.6/repodata/repomd.xml

  Update repository (saved config file used for OBS site URL, repositories, architectures):
obs_repos -r ./repos_cache_15.6 -a "armv7hl noarch" d -s 1_000_000

  Show packages with packet relations filter (from cache):
obs_repos -r ./repos_cache_15.6 f --exclude-arch "aarch64 x86_64" --requires "libtimed"

  Show show list of architectures of all packages:
obs_repos -r ./repos_cache_15.6 a -c

  Show show list of architectures with package name filter:
obs_repos -r ./repos_cache_15.6 -p "connman" a -c

  Show packages that contains "*connmand*" file:
obs_repos -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f -AVM --files "connmand"

  Show files table from packages that contains "*connmand*" files:
obs_repos -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --files "connmand" --out files

  Show files table from packages that contains binary files:
obs_repos -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --out files --files "^/bin/ ^/usr/bin/"

  Show provides table from packages that contains "(" in provides:
obs_repos -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --out provides --provides "("

Advanced usage:

  Update meta cache without download (saved config file used for OBS site URL, repositories, architectures):
obs_repos -r ./.repos_cache_15.6 d --keep-conf --dummy

  Show repositories versions from meta cache:
obs_repos -vv -r ./.repos_cache_15.6 a

  Show packages from primary .xml.gz file (repository low level API):
obs_repos pr --path ./repos_cache_15.6/apps/repodata/primary.xml.gz

  Show packages from filelists .xml.gz file (repository low level API):
obs_repos fl --path ./repos_cache_15.6/apps/repodata/filelists.xml.gz
```
