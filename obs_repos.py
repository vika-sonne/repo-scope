#!/usr/bin/env python3

from typing import Iterator
from math import pow
from xml.dom import minidom
from xml.dom.minidom import Document, Element, Text
from xml.dom.minicompat import NodeList
import gzip
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tomllib import load as load_toml
from dataclasses import dataclass, asdict
import pickle
import requests


VERSION = '2025.5'

DEFAULT_REPOS_CACHE = '.repos_cache'
DOWNLOAD_LOG_FILE_NAME = '.download.log'
CONF_TOML_FILE_NAME = '.conf.toml'
PACKAGES_CACHE_FILE_NAME = '.packages.bin'
DEVEL_PACKAGE_NAMES = ('-devel', '-test', '-tests', '-debuginfo', '-debugsource', 'linux-headers', '-sysroot')
PACKAGE_CACHE_HEADER = ('PACKAGE_CACHE', '2025.5')  # magic, version


class MissingArgument(Exception): pass


class Relation:
	__slots__ = ('name', 'flags', 'ver', 'rel')
	def __init__(self, name, flags, ver, rel) -> None:
		self.name, self.flags = name, flags
		self.ver, self.rel = ver, rel
	def __str__(self) -> str:
		return ' '.join((self.name, self.flags or '', self.ver or '', self.rel or '')).rstrip()
	def __eq__(self, other):
		if self.name == other.name:
			return True
		return False
	def provides(self, other: 'Relation | str'):
		if isinstance(other, Relation):
			if self.name == other.name:
				return True
		elif isinstance(other, str):
			if self.name == other:
				return True
		return False


class Package:
	__slots__ = ('name', 'arch', 'version', 'rel', 'files', 'href', 'provides', 'requires', 'summary', 'description', 'size', 'size_installed', 'repo')

	def __init__(self, name: str, arch: str, version: str, rel: str,
			files: tuple[str] | None = None,
			href: str | None = None,
			provides: tuple[Relation] | None = None,
			requires: tuple[Relation] | None = None,
			summary: str | None = None,
			description: str | None = None,
			size: int = 0, size_installed: int = 0,
			repo: str | None = None) -> None:
		self.name, self.arch = name, arch
		self.version, self.rel = version, rel
		self.files = files
		self.href = href
		self.provides, self.requires = provides, requires
		self.summary, self.description = summary, description if summary != (description or '').rstrip('.') else None
		self.size, self.size_installed = size, size_installed
		self.repo = repo

	def __str__(self):
		return self.to_str(True)

	def __eq__(self, other):
		if self.name == other.name and self.arch == other.arch and self.version == other.version:
			return True
		return False

	def __hash__(self):
		return hash((self.repo, self.name, self.arch, self.version))

	def iter_relations(self, provides: 'Package', reverse = False) -> Iterator[tuple[Relation, Relation | None]]:
		'iters: (Relation, Relation) self.requires->provides.provides; (Relation, None) to file self.requires->provides.files'
		if reverse:
			for require in provides.requires or []:
				if require.name.startswith('/'):
					for file_path in self.files or []:
						if file_path == require.name:
							yield require, None
				else:
					for provide in self.provides or []:
						if provide.provides(require):
							yield require, provide
		else:
			for require in self.requires or []:
				if require.name.startswith('/'):
					# iters: Relation, None # Relation to package file
					for file_path in provides.files or []:
						if file_path == require.name:
							yield require, None
				else:
					# iters: Relation, Relation
					for provide in provides.provides or []:
						if require.provides(provide):
							yield require, provide

	def is_provides(self, provides: tuple[str]) -> bool:
		'returns True if packet provides relations; used for filters'
		for provide_ in provides or []:
			if provide_.startswith('/'):
				# relation to file
				for file_path in self.files or []:
					if file_path == provide_:
						return True
			else:
				# relation to relation
				for provide in self.provides or []:
					if provide_ in provide.name:
						return True
		return False

	def is_requires(self, requires: tuple[str]) -> bool:
		'returns True if packet requires relations; used for filters'
		for require_ in requires or []:
			for require in self.requires or []:
				if require_ in require.name:
					return True
		return False

	def iter_files(self, file_path_filters: tuple[str] | None = None) -> Iterator[str]:
		'iterates package files according filename filter'
		if self.files:
			if file_path_filters:
				# return files according filter
				for file_path in self.files:
					if self.is_text_filtered(file_path, file_path_filters):
						yield file_path
			else:
				# return files
				for file in self.files:
					yield file

	def has_files(self, file_name_filters: tuple[str]) -> bool:
		return any(self.iter_files(file_name_filters))

	def to_str(self, arch = False, version = False, file = False, summary = False, relations = False,
			files = False, files_filter: tuple[str] | None = None, size = False, repo = True) -> str:
		'returns multi-line string with package optional info'
		return ' '.join(filter(lambda x: x, (self.repo if self.repo and repo else '', self.name, self.arch if arch else '',
			self.version if version else '', self.rel if version else '', (self.href or '') if file else '',
			f'({format_size(self.size)})' if size else '',
			'\n\t' + '\n\t'.join(filter(lambda x: x, (self.summary, self.description))) if summary else '',
			'\n\t' + '\n\t'.join(self.iter_files(files_filter)) if files and self.files else '',
			'\n\tPROVIDES:' if relations and self.provides else '',
			'\n\t' + '\n\t'.join(tuple(map(str, self.provides))) if relations and self.provides else '',
			'\n\tREQUIRES:' if relations and self.requires else '',
			'\n\t' + '\n\t'.join(tuple(map(str, self.requires))) if relations and self.requires else '',
			)))

	@classmethod
	def is_text_filtered(cls, text: str, filters: tuple[str] | None) -> bool:
		'returns False - not passed'
		if not filters:
			return True  # no file filter
		for filter in filters:
			if filter.startswith('^'):
				if text.startswith(filter[1:]):
					return True
			elif filter in text:
				return True
		return False


class Repomd:
	__slots__ = ('revision', 'primary_url', 'filelists_url')
	def __init__(self, revision: str, primary_url: str | None = None, filelists_url: str | None = None):
		self.revision, self.primary_url, self.filelists_url = revision, primary_url, filelists_url
	def __str__(self):
		return f'revision={self.revision} primary_url={self.primary_url} filelists_url={self.filelists_url}'


class PackageNameFilter:
	'filter by package name pattern'


	class FilterBase:
		def __init__(self, pattern: str):
			self.is_inverse = pattern.startswith('!')
			self.pattern = pattern[1:] if self.is_inverse else pattern
		def is_match(self, name: str) -> bool:
			return False


	class FilterExactly(FilterBase):
		def is_match(self, name: str) -> bool:
			return name == self.pattern


	class FilterParts(FilterBase):
		def __init__(self, pattern: str):
			super().__init__(pattern)
			self.name_start = pattern + '-'
			self.name_mid = '-' + pattern + '-'
			self.name_end = '-' + pattern
		def is_match(self, name: str) -> bool:
			return name.startswith(self.name_start) or name.endswith(self.name_end) or self.name_mid in name or \
				name == self.pattern


	class FilterText(FilterBase):
		def is_match(self, name: str) -> bool:
			return self.pattern in name


	class FilterStartswith(FilterBase):
		def is_match(self, name: str) -> bool:
			return name.startswith(self.pattern)


	class FilterEndswith(FilterBase):
		def is_match(self, name: str) -> bool:
			return name.endswith(self.pattern)


	def __init__(self, pattern: str):
		self.filters: list[FilterBase] = []
		for name_pattern_ in pattern.split(' '):
			match name_pattern_[0]:
				case '=':
					self.filters.append(self.FilterExactly(name_pattern_[1:]))
				case '~':
					self.filters.append(self.FilterParts(name_pattern_[1:]))
				case '^':
					self.filters.append(self.FilterStartswith(name_pattern_[1:]))
				case '#':
					self.filters.append(self.FilterEndswith(name_pattern_[1:]))
				case _:
					self.filters.append(self.FilterText(name_pattern_))
		self.is_all_inverse = all(x.is_inverse for x in self.filters)

	def is_match(self, name: str) -> bool:
		for filter in self.filters:
			is_match = filter.is_match(name)
			if filter.is_inverse:
				if is_match:
					return False
			elif is_match:
				return True
		return self.is_all_inverse


class PackageSummaryFilter:
	'filter by package summary and description pattern'

	def __init__(self, pattern: str):
		if pattern.startswith('~'):
			self.pattern = pattern[1:].lower()
			self.is_case_insensitive = True
		else:
			self.pattern = pattern
			self.is_case_insensitive = False

	def is_match(self, package: Package) -> bool:
		return any(self.pattern in (x.lower() if self.is_case_insensitive else x) for x in (package.summary, package.description) if x)


def format_size(val: int) -> str:
	MULTIPLIERS = { 'GB': 9, 'MB': 6, 'kB': 3, 'B': 0, }  # suffix, power of 10
	for suffix, power_10 in MULTIPLIERS.items():
		if val >= pow(10, power_10) or power_10 == 0:
			return f'{val / pow(10, power_10):{'.0f' if power_10 == 0 else '.1f'}} {suffix}'

def _get_tag_value_text(node: NodeList | Element | Text) -> str | None:
	if issubclass(type(node), NodeList):
		return _get_tag_value_text(node[0])
	elif issubclass(type(node), Element):
		if node.childNodes:
			return _get_tag_value_text(node.childNodes[0])
	elif issubclass(type(node), Text):
		return node.nodeValue
	return None

def _get_tag_value_text2(node, name: str) -> str | None:
	if (node2 := node.getElementsByTagName(name)):
		return _get_tag_value_text(node2[0])
	return None

def _open_xml_file(file_path: str) -> Document | None:
	'returns xml document from .xml.gz file'
	match file_path.lower():
		case p if p.endswith('.xml'): open_ = open
		case p if p.endswith('.xml.gz'): open_ = gzip.open
		case _:
			raise NotImplementedError  # support .xml or .xml.gz files only
	try:
		with open_(file_path, 'rb') as f:
			return minidom.parseString(f.read())
	except FileNotFoundError: return None

def iter_filelist(root: Document) -> Iterator[Package]:
	'parse filelist .xml: packages (name, files)'

	for package in root.getElementsByTagName('package'):
		version = package.getElementsByTagName('version')[0]
		version, rel = version.getAttributeNode('ver').value, version.getAttributeNode('rel').value
		files = []
		for f in package.getElementsByTagName('file'):
			files.append(_get_tag_value_text(f))
		yield Package(package.getAttribute('name'), package.getAttribute('arch'), version, rel, files)

def show_filelist(file_path: str):
	'parse filelist .xml file: packages (name, files)'

	for i, package in enumerate(iter_filelist(_open_xml_file(file_path))):
		print(i+1, package)

def iter_primary(root: Document, add_summary: bool = False) -> Iterator[Package]:
	'parse primary .xml file: packages (name, provides, requires)'

	def get_attr(node, attr_name: str) -> str | None:
		try:
			return node.getAttributeNode(attr_name).value
		except AttributeError:
			return None

	for package in root.getElementsByTagName('package'):
		_type = package.getAttributeNode('type').value
		if _type != 'rpm':
			raise Exception(f'_type != "rpm": {_type}')
		name = _get_tag_value_text(package.getElementsByTagName('name')[0])
		arch = _get_tag_value_text(package.getElementsByTagName('arch')[0])
		summary = _get_tag_value_text(package.getElementsByTagName('summary')[0]) if add_summary else None
		description = _get_tag_value_text2(package, 'description') if add_summary else None
		version = package.getElementsByTagName('version')[0]
		version, rel = version.getAttributeNode('ver').value, version.getAttributeNode('rel').value
		size = package.getElementsByTagName('size')[0]
		size, size_installed = map(int, (size.getAttributeNode('package').value, size.getAttributeNode('installed').value))
		location_href = package.getElementsByTagName('location')[0].getAttributeNode('href').value
		format = package.getElementsByTagName('format')[0]
		provides, requires = [], []
		for entry in format.getElementsByTagName('rpm:provides')[0].getElementsByTagName('rpm:entry'):
			if entry.hasAttribute('flags'):
				provides.append(Relation(entry.getAttributeNode('name').value,
					entry.getAttributeNode('flags').value,
					entry.getAttributeNode('ver').value,
					get_attr(entry, 'rel')))
			else:
				provides.append(Relation(entry.getAttributeNode('name').value, None, None, None))
		if (_requires := format.getElementsByTagName('rpm:requires')) and _requires.length:
			for entry in _requires[0].getElementsByTagName('rpm:entry'):
				if entry.hasAttribute('flags'):
					requires.append(Relation(
						entry.getAttributeNode('name').value,
						entry.getAttributeNode('flags').value,
						entry.getAttributeNode('ver').value,
						get_attr(entry, 'rel')))
				else:
					requires.append(Relation(entry.getAttributeNode('name').value, None, None, None))
		yield Package(name, arch, version, rel, None, location_href, provides, requires, summary, description, size, size_installed)

def show_primary(file_path: str):
	'parse primary .xml file: packages (name, provides, requires)'

	for i, package in enumerate(iter_primary(_open_xml_file(file_path))):
		print(i+1, package)

def get_repomd(root: Document | None) -> Repomd | None:
	'parse xml document from repomd.xml file'
	if not root:
		return None
	revision = _get_tag_value_text(root.getElementsByTagName('revision'))
	if not revision:
		return None
	repomd = Repomd(revision)
	for i, data in enumerate(root.getElementsByTagName('data')):
		_type = data.getAttributeNode('type').value
		if _type == 'primary':
			repomd.primary_url = data.getElementsByTagName('location')[0].getAttributeNode('href').value
		elif _type == 'filelists':
			repomd.filelists_url = data.getElementsByTagName('location')[0].getAttributeNode('href').value
	return repomd

def show_repomd(file_path: str):
	'parse xml document from repomd.xml file'
	if (repomd := get_repomd(_open_xml_file(file_path))):
		print(repomd)
	else:
		print(f'NOT VALID REPOMD XML FILE: {file_path}')

def iter_repositories_repomds(file_path: str) -> Iterator[tuple[Repomd, Path]]:
	'iters (repomd.xml document, repo path) from repositories path with repositories tree: main, oss, non-oss'
	for repository_path in (x for x in Path(file_path).iterdir() if x.is_dir()):
		if (repomd := get_repomd(_open_xml_file(str(repository_path.joinpath('repodata', 'repomd.xml'))))):
			yield repomd, repository_path

def iter_repositories_packages(repos_path: str, add_summary: bool = False, file_list: bool = False) -> Iterator[Package]:
	'iters packages from repositories path with repositories tree: main, oss, non-oss'
	for repomd, repository_path in iter_repositories_repomds(repos_path):
		# print(f'{str(repository_path.joinpath(repomd.primary_url))=}')
		if file_list:
			for package in iter_filelist(_open_xml_file(str(repository_path.joinpath(repomd.filelists_url)))):
				package.repo = repository_path.name
				yield package
		else:
			for package in iter_primary(_open_xml_file(str(repository_path.joinpath(repomd.primary_url))), add_summary):
				package.repo = repository_path.name
				yield package

if __name__ == '__main__':
	from sys import argv, exit
	from argparse import ArgumentParser, RawTextHelpFormatter
	from pathlib import Path

	def main():

		def parse_args():
			parser = ArgumentParser(
				formatter_class=RawTextHelpFormatter,
				description=f'''OBS (Open Build Service) repository tool: download/mirror and analyse .rpm based repositories.
Version {VERSION}

Useful for repositories (at least but not at last):
  openSUSE http://download.opensuse.org/tumbleweed/repo/oss/
  Sailfish https://repo.sailfishos.org/obs/sailfishos:/

This pure python utility requires at least python 3.12.
''',
				epilog=f'''
Examples:

  Download repositories "apps system games" for architectures "armv7hl noarch" from OBS site to cache path ".repos_cache_15.6" with package download size-max filter:
{Path(argv[0]).name} -r ./repos_cache_15.6 -a "armv7hl noarch" d -u 'https://example.org/{{repo}}:/15.6/' -e "apps system games" -s 1_000_000
  Will be downloaded 3 repositories by URLs:
  https://example.org/apps:/15.6/repodata/repomd.xml
  https://example.org/system:/15.6/repodata/repomd.xml
  https://example.org/games:/15.6/repodata/repomd.xml

  Update repository (saved config file used for OBS site URL, repositories, architectures):
{Path(argv[0]).name} -r ./repos_cache_15.6 -a "armv7hl noarch" d -s 1_000_000

  Show packages with packet relations filter (from cache):
{Path(argv[0]).name} -r ./repos_cache_15.6 f --exclude-arch "aarch64 x86_64" --requires "libtimed"

  Show show list of architectures of all packages:
{Path(argv[0]).name} -r ./repos_cache_15.6 a -c

  Show show list of architectures with package name filter:
{Path(argv[0]).name} -r ./repos_cache_15.6 -p "connman" a -c

  Show packages that contains "*connmand*" file:
{Path(argv[0]).name} -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f -AVM --files "connmand"

  Show files table from packages that contains "*connmand*" files:
{Path(argv[0]).name} -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --files "connmand" --out files

  Show files table from packages that contains binary files:
{Path(argv[0]).name} -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --out files --files "^/bin/ ^/usr/bin/"

  Show provides table from packages that contains "(" in provides:
{Path(argv[0]).name} -r ./repos_cache_15.6 --exclude-devel -a "armv7hl" f --out provides --provides "("

Advanced usage:

  Update meta cache without download (saved config file used for OBS site URL, repositories, architectures):
{Path(argv[0]).name} -r ./.repos_cache_15.6 d --keep-conf --dummy

  Show repositories versions from meta cache:
{Path(argv[0]).name} -vv -r ./.repos_cache_15.6 a

  Show packages from primary .xml.gz file (repository low level API):
{Path(argv[0]).name} pr --path ./repos_cache_15.6/apps/repodata/primary.xml.gz

  Show packages from filelists .xml.gz file (repository low level API):
{Path(argv[0]).name} fl --path ./repos_cache_15.6/apps/repodata/filelists.xml.gz
''',
			)

			parser.add_argument('-v', '--verbose', action='count', default=0,
				help='verbose level; example: -vvv; default: none')
			parser.add_argument('--version', action='version', version=VERSION)
			parser.add_argument('-r', '--repos-path', metavar='PATH', default=DEFAULT_REPOS_CACHE,
				help=f'repositories path; default: {Path(DEFAULT_REPOS_CACHE).absolute()}')
			parser.add_argument('-p', '--package', metavar='NAME',
				help='package filter by name, space separated values; = exactly, ~ part, ^ starts with, # ends with, ! not; examples: "timed", "^timed #!-doc", "!timed", "=!timed", "~!timed", "^!timed", "#!timed"')
			parser.add_argument('-a', '--arch', metavar='NAME',
				help='package filter by architecture, space separated values; example: "aarch64 armv7hl x86_64 noarch src"')
			parser.add_argument('-x', '--exclude-arch', metavar='NAME',
				help='package filter by architecture, space separated values; example: "aarch64 armv7hl x86_64"')
			parser.add_argument('-d', '--exclude-devel', action='store_true', help='package filter for test/debug/devel')

			subparsers = parser.add_subparsers(dest='subparser', help='subcommands')

			parser_d = subparsers.add_parser('download', aliases=('d',),
				help='download repositories meta files from OBS to cache; to download .rpm files use -a/--arch option')
			parser_d.add_argument('-u', '--url', metavar='URL',
				help='URL template to download repositories; example: https://example.org/{repo}:/')
			parser_d.add_argument('-e', '--repos', metavar='NAMES',
				help='repositories names; example: "system games"')
			parser_d.add_argument('-s', '--size-max', metavar='NUM', type=int,
				help='package filter by download size, bytes; example: 1_000_000')
			parser_d.add_argument('-S', '--size-min', metavar='NUM', type=int,
				help='package filter by download size, bytes; example: 2_097_152')
			parser_d.add_argument('--keep-meta', action='store_true',
				help='do not update meta files but download missing meta files for new repositories')
			parser_d.add_argument('--keep-conf', action='store_true', help=f'do not update config "{CONF_TOML_FILE_NAME}" file')
			parser_d.add_argument('--keep-cache', action='store_true',
				help=f'do not update meta cache "{PACKAGES_CACHE_FILE_NAME}" file')
			parser_d.add_argument('-R', '--redownload', action='store_true',
				help='download packages but keep existing valid files; combines following options: --keep-meta, --keep-conf, --keep-cache')
			parser_d.add_argument('-D', '--dummy', action='store_true', help='do not download: nor meta, nor packages')

			parser_a = subparsers.add_parser('architectures', aliases=('a',), help='show architectures data')
			parser_a.add_argument('-C', action='store_false', help='hide archs counter')
			parser_a.add_argument('-c', '--count', action='store_true', help='show packages count for each architecture')

			parser_f = subparsers.add_parser('filter', aliases=('f',),
				help='process repositories from cache by various filters (main usage)')
			parser_f.add_argument('--provides', metavar='NAME',
				help='package filter by provides, space separated values; examples: "libtimed", "libc.so.6(GLIBC_2.34) libtimed"')
			parser_f.add_argument('--requires', metavar='NAME', help='package filter by requires; example: libtimed')
			parser_f.add_argument('--files', metavar='NAME',
				help='package filter by files, space separated values; ^ starts with; examples: "libtimed", "libc.so.6 libtimed", "^/bin/"')
			parser_f.add_argument('--summary', metavar='TEXT',
				help='package filter by summary and description; examples: "MDM", case insensitive: "~mdm"')
			parser_f.add_argument('-C', action='store_false', help='hide packets counter')
			parser_f.add_argument('-N', action='store_false', help='hide repository name')
			parser_f.add_argument('-A', action='store_true', help='show packets architecture info')
			parser_f.add_argument('-F', action='store_true', help='show packets file info')
			parser_f.add_argument('-V', action='store_true', help='show packets version info')
			parser_f.add_argument('-D', action='store_true', help='show packets summary and description info')
			parser_f.add_argument('-R', action='store_true', help='show packets relations info')
			# parser_f.add_argument('-T', action='store_true', help='show packets relations filtered by --provides, --requires options')
			parser_f.add_argument('-L', action='store_true', help='show packets files info')
			parser_f.add_argument('-M', action='store_true', help='show packets files filtered by --files option')
			parser_f.add_argument('-Z', action='store_true', help='show packets size')
			parser_f.add_argument('--out', default='text',
				choices=('text', 'files', 'files-full', 'provides', 'provides-full', 'tree', 'tree-full', 'rtree', 'rtree-full'),
				help='output format: files/files-full/provides/provides-full - sorted table; tree/rtree - packet-based /reverse; tree-full/rtree-full - relation-based /reverse; default: text')

			parser_fl = subparsers.add_parser('filelists', aliases=('fl',), help='show filelists files data')
			parser_fl.add_argument('-p', '--path', metavar='FILE_PATH', nargs='*',
				help=f'.xml.gz file path; example: filelists.xml.gz')

			parser_fl = subparsers.add_parser('primary', aliases=('pr',), help='show primary files data')
			parser_fl.add_argument('-p', '--path', metavar='FILE_PATH', nargs='*',
				help=f'.xml.gz file path; example: primary.xml.gz')

			return parser.parse_args()

		def print_package(package: Package, i: int | None = None):
			'print package info according to comman-line options'
			print(*filter(lambda x: x, (
				None if not args.C or i is None else i,
				package.to_str(args.A, args.V, args.F, args.D, args.R, args.L or args.M, files_filter if args.M else None, args.Z, args.N),
				)))

		def print_repos_versions(repos_versions: dict[str, str]):
			print('\trepos versions...')
			max_repos_names_len = max((len(x) for x in repos_versions.keys()))
			for k, v in repos_versions.items():
				print(f'\t\t{k:>{max_repos_names_len}}: {v}')

		def load_meta_cache() -> tuple[dict[str, str], tuple[Package]]:
			'returns (repos_versions, packages) from meta cache'

			def show_help():
				print('\tWRONG cache format. Please, refresh cache use command line:')
				print(f'{Path(argv[0]).name} -r "{str(repos_path.absolute())}" d --dummy')

			# load packages from cache # deserialize packages from binary file
			if args.verbose:
				print('Load packages cache...')
			try:
				with open(str(repos_path.joinpath(PACKAGES_CACHE_FILE_NAME).absolute()), 'rb') as f:
					packages = pickle.load(f)
			except FileNotFoundError:
				show_help()
				exit(-1)
			# deserialize: magic header, repositories versions, packages
			# check magic and packages
			if not isinstance(packages, tuple) or len(packages) != 3 \
					or packages[0] != PACKAGE_CACHE_HEADER \
					or not isinstance(packages[1], dict) \
					or not isinstance(packages[2], tuple):
				show_help()
				exit(-1)
			if args.verbose:
				if args.verbose > 1:
					print(f'\theader: {', '.join(packages[0])}')
					print_repos_versions(packages[1])
				print(f'\tpackages: {len(packages[2]):_}')
				if args.verbose > 1:
					print(f'\tfiles: {sum((len(x.files) for x in packages[2] if x.files)):_}')
			return packages[1:]

		def load_packages_cache() -> tuple[Package]:
			'returns packages from meta cache'
			return load_meta_cache()[1]

		def iter_packages() -> Iterator[Package]:
			'iterates packages from cache with filters by name, arch'
			packages = load_packages_cache()
			for package in packages:
				# filter package
				if (exclude_arch and package.arch in exclude_arch) or (arch and package.arch not in arch):
					# package arch filter
					continue
				if args.exclude_devel:
					# package filter for test/debug/devel
					if any((x in package.name for x in DEVEL_PACKAGE_NAMES)):
						continue
				if package_name_filter and not package_name_filter.is_match(package.name):
					# package name filter
					continue
				# yield package
				yield package

		args = parse_args()

		if args.verbose:
			print(' '.join(argv))

		# packets filters
		arch: tuple[str] | None = tuple(args.arch.split(' ')) if args.arch else None
		exclude_arch: tuple[str] | None = tuple(args.exclude_arch.split(' ')) if args.exclude_arch else None
		package_name_filter = PackageNameFilter(args.package) if args.package else None
		# repositories path
		repos_path = Path(args.repos_path)

		match args.subparser:

			case 'download' | 'd':
				# download repos by URL template # update .conf and packages cache

				@dataclass
				class Conf:
					url: str
					repos: tuple[str]
					arches: tuple[str]

					@classmethod
					def load_from_toml(cls, file_path: str) -> 'Conf':

						def space_sep(val: str) -> tuple[str] | None:
							'returns space separated values or None'
							if val:
								return tuple(val.split(' '))
							return None

						# read .toml file
						buff = {}
						try:
							with open(file_path, 'rb') as f:
								buff = load_toml(f)
						except FileNotFoundError: pass
						# create Conf
						try:
							return Conf(
								args.url or buff['url'],
								space_sep(args.repos) or buff['repos'],
								space_sep(args.arch) or buff.get('arch', tuple()),
								)
						except (TypeError, KeyError): raise MissingArgument

					def save_to_toml(self, file_path: str):
						with open(file_path, 'w') as f:
							for k, v in asdict(self).items():
								if issubclass(type(v), str):
									f.write(f'{k} = "{v}"\n')
								elif issubclass(type(v), (tuple, list)):
									f.write(f'{k} = [{','.join('"'+x+'"' for x in v)}]\n')

				def download_file(url: str) -> bytes | None:
					h_response = session.get(url)
					if h_response.status_code != 200:
						buff = f'Can\'t download URL: status_code={h_response.status_code} {url}'
						log(buff)
						return None
					return h_response.content

				def download_repomd(url: str) -> tuple[Document, bytes] | None:
					if (buff := download_file(url)):
						if (repomd := get_repomd(minidom.parseString(buff))):
							return (repomd, buff)
					return None

				def download_and_save_file(url: str, repo_path: Path, path: str, overwrite=True,
						package: Package | None = None, prefix: str | None = None) -> bool:
					'download file from url+path url to repo_path+path'
					url += path
					path = repo_path.joinpath(path)
					log(f'{prefix+' ' if prefix else ''}{str(path.absolute())[len(str(repos_path.absolute()))+1:]}{f' ({format_size(package.size)})' if package else ''}')
					if not overwrite and path.exists():
						# file exists # try verify file to keep it
						if package and path.stat().st_size != package.size:
							log(f'\tfile corrupted (size {path.stat().st_size:_} != expected {package.size:_}) {package.href}')
						else:
							# keep file
							log('\tkeep')
							return True
					# download file
					log(f'\t{url}')
					if not args.dummy:
						path.parent.mkdir(exist_ok=True, parents=True)
						if (buff := download_file(url)):
							with open(path.absolute(), 'wb') as f:
								f.write(buff)
					return True

				def log(msg: str):
					if args.verbose:
						print(msg)
					log_f.write('\t')
					log_f.write(msg)
					log_f.write('\n')

				if args.redownload:
					args.keep_meta = args.keep_conf = args.keep_cache = True

				# download repos meta files: repodata
				session = requests.Session()
				repos_path.mkdir(exist_ok=True, parents=True)
				with open(str(repos_path.joinpath(DOWNLOAD_LOG_FILE_NAME).absolute()), 'a') as log_f:
					# write log timestamp
					log_f.write(datetime.now(timezone.utc).replace(microsecond=0).astimezone().isoformat())
					log_f.write('\n')
					log_f.write('\t'+' '.join(argv))
					log_f.write('\n')

					if args.dummy:
						log('Dummy - not download !')

					# load config from .toml file
					try:
						conf = Conf.load_from_toml(str(repos_path.joinpath(CONF_TOML_FILE_NAME).absolute()))
					except MissingArgument:
						print('Missing arguments: --url, --repos\nUse -h for help message')
						exit(-1)

					# download repos meta files
					if not (args.keep_meta or args.dummy):
						if args.verbose:
							print('\tDOWNLOAD repositories meta files')
						for repo in conf.repos:
							repo_path, repo_url = repos_path.joinpath(repo), conf.url.format(repo=repo)
							# download repomd.xml file
							repomd_path = 'repodata/repomd.xml'
							# check repository for newest version
							if not args.dummy:
								repomd_version_current = None
								if (repomd := get_repomd(_open_xml_file(str(repo_path.joinpath(repomd_path).absolute())))):
									repomd_version_current = repomd.revision
								if (buff := download_repomd(repo_url+repomd_path)):
									repomd, buff = buff
									if repomd_version_current is None or repomd_version_current != repomd.revision:
										# is new repository version
										if repomd_version_current:
											# save previous repomd.xml version
											log(f'\tNew version of repo {repo}: {repomd.revision} current version {repomd_version_current}')
											repo_path.joinpath(repomd_path).rename(
												repo_path.joinpath(repomd_path+'.'+repomd_version_current))
										else:
											log(f'\tNew version of repo {repo}: {repomd.revision}')
										# save new repomd.xml version
										repo_path.joinpath(repomd_path).parent.mkdir(exist_ok=True, parents=True)
										with open(str(repo_path.joinpath(repomd_path).absolute()), 'wb') as f:
											f.write(buff)
								else:
									log(f'CAN\'T PARSE repo {repo}: {repo_url+repomd_path}')
									continue
							# download primary and filelists files
							if download_and_save_file(repo_url, repo_path, repomd_path):
								if (repomd := get_repomd(_open_xml_file(str(repo_path.joinpath(repomd_path).absolute())))):
									download_and_save_file(repo_url, repo_path, repomd.filelists_url, not args.keep_meta)
									download_and_save_file(repo_url, repo_path, repomd.primary_url, not args.keep_meta)
							log_f.flush()

					# save config to .toml file
					if not args.keep_conf:
						conf.save_to_toml(str(repos_path.joinpath(CONF_TOML_FILE_NAME).absolute()))

					# update meta cache # serialize packages and save to binary file
					if not args.keep_cache:
						log('Update meta cache...')
						if args.verbose > 1:
							log(f'\theader: {', '.join(PACKAGE_CACHE_HEADER)}')
						packages_cache, count, packages = [], 0, {}
						# read repos versions
						repos_versions: dict[str, str] = dict()  # dict[repo_name, version]
						for repomd, repo_path in iter_repositories_repomds(str(repos_path.absolute())):
							repos_versions[repo_path.name] = repomd.revision
						if args.verbose:
							print_repos_versions(repos_versions)
						# read packages
						log('\tadd packages...')
						for package in iter_repositories_packages(str(repos_path.absolute()), True):
							packages_cache.append(package)
							packages[package] = package
							count += 1
						log(f'\t\tpackages: {count:_}')
						# read packages files
						log('\tadd files...')
						files_count = 0
						for package_files in iter_repositories_packages(str(repos_path.absolute()), file_list=True):
							packages[package_files].files = package_files.files  # add files to package
							files_count += len(package_files.files)
						log(f'\t\tfiles: {sum((len(x.files) for x in packages_cache if x.files)):_}')
						# save meta cache # serialize to binary file
						with open(str(repos_path.joinpath(PACKAGES_CACHE_FILE_NAME).absolute()), 'wb') as f:
							# serialize: magic header, repositories versions, packages
							pickle.dump((PACKAGE_CACHE_HEADER, repos_versions, tuple(packages_cache)), f)
						del packages

					# download repos packages # use packages cache
					if conf.arches and not args.dummy:
						def _iter_packages() -> Iterator[Package]:
							for package in iter_packages():
								# filter package
								if size_max and package.size > size_max:
									# package filter by size, bytes
									continue
								if size_min and package.size < size_min:
									# package filter by size, bytes
									continue
								if package.repo not in conf.repos:
									# package filter by repository
									continue
								yield package

						size_max = int(args.size_max or 0)  # package filter
						size_min = int(args.size_min or 0)  # package filter
						packages = tuple(_iter_packages())  # packages to download according filters
						start_time, downloaded_size, download_total = datetime.now(), 0, sum(x.size for x in packages)
						template_str = f'{{time}} {{packages_count:{len(str(len(packages)))}}} / {len(packages)} ({{downloaded_size}} / {{remainimg_size}})'
						log('DOWNLOAD packages:')
						log(f'\t       repos: {', '.join(conf.repos)}')
						log(f'\t      arches: {', '.join(conf.arches)}')
						log(f'\tcount (size): {len(packages)} ({format_size(sum(x.size for x in packages))})')
						for i, package in enumerate(packages):
							# download package file
							repo_url = conf.url.format(repo=package.repo)
							time_ = timedelta(seconds=(datetime.now() - start_time).seconds)
							if package.href:
								download_and_save_file(repo_url, repos_path.joinpath(package.repo), package.href, False, package,
									template_str.format(time=time_, packages_count=i+1, downloaded_size=format_size(downloaded_size),
							 			remainimg_size=format_size(download_total-downloaded_size)))
								downloaded_size += package.size
							log_f.flush()
						if packages:
							log(f'\tDOWNLOADED packages: {i+1} / {timedelta(seconds=(datetime.now() - start_time).seconds)}')

			case 'architectures' | 'a':
				if args.count:
					# show table: arches and packages count # count packages
					archs = {}
					for package in iter_packages():
						archs[package.arch] = archs.get(package.arch, 0) + 1
					# show table
					template = f'{{i}} {{arch:{max(len(x) for x in archs.keys())}}} {{count:{len(str(max(archs.values())))}}}'
					for i, (arch, count) in enumerate(sorted(archs.items(), key=lambda x: x[0])):
						if args.C:
							print(template.format(i=i+1, arch=arch, count=count))
						else:
							print(arch, count)
				else:
					# count packages
					archs = set()
					for package in iter_packages():
						archs.add(package.arch)
					# show arches
					for i, arch in enumerate(sorted(archs)):
						if args.C:
							print(i+1, arch)
						else:
							print(arch)

			case 'filter' | 'f':
				summary_filter = PackageSummaryFilter(args.summary) if args.summary else None
				provides_filter: tuple[str] | None = args.provides.split(' ') if args.provides else None
				requires_filter: tuple[str] | None = args.requires.split(' ') if args.requires else None
				files_filter: tuple[str] | None = args.files.split(' ') if args.files else None

				if args.out == 'dot' and not package_name_filter:
					print(f'Out format "dot" allowed only for one package. Define package name flter. See help: {argv[0]} -h')
					exit(-1)

				def _iter_packages() -> Iterator[Package]:
					'iters packages according filters'
					for package in iter_packages():
						# filter package
						if summary_filter and not summary_filter.is_match(package):
							# package summary and description filter
							continue
						if files_filter and not package.has_files(files_filter):
							# package files filter
							continue
						if provides_filter and not package.is_provides(provides_filter):
							# package provides filter
							continue
						if requires_filter and not package.is_requires(requires_filter):
							# package requires filter
							continue
						# yield package
						yield package

				if args.repos_path:
					# read repositories path # show packages from repositories
					match args.out:
						case 'files' | 'files-full':
							# print sorted from all packages files according to filters
							files_full = args.out == 'files-full'
							file_path_max_len = repo_max_len = package_name_max_len = 0
							files: dict[str, Package] = dict()
							for package in _iter_packages():
								repo_max_len = max(repo_max_len, len(package.repo))
								package_name_max_len = max(package_name_max_len, len(package.name))
								for file_path in package.files or []:
									# add package files # apply files filter
									if not package.is_text_filtered(file_path, files_filter):
										continue  # file not passed
									files[file_path] = package
									file_path_max_len = max(file_path_max_len, len(file_path))
							# print files with packages
							for file_path, package in sorted(files.items()):
								if files_full:
									print(f'{file_path:{file_path_max_len}}\t{package.repo:{repo_max_len}} {package.name:{package_name_max_len}}\t{package.arch}\t{package.version}')
								else:
									print(f'{file_path:{file_path_max_len}}\t{package.repo:{repo_max_len}} {package.name}')

						case 'provides' | 'provides-full':
							# print sorted from all packages provides according to filters
							provides_full = args.out == 'provides-full'
							provide_path_max_len = repo_max_len = package_name_max_len = 0
							provides: dict[str, Package] = dict()
							for package in _iter_packages():
								repo_max_len = max(repo_max_len, len(package.repo))
								package_name_max_len = max(package_name_max_len, len(package.name))
								for provide in package.provides or []:
									# add package provides # apply provides filter
									if not package.is_text_filtered(provide.name, provides_filter):
										continue  # file not passed
									provides[provide.name] = package
									provide_path_max_len = max(provide_path_max_len, len(provide.name))
							# print provides with packages
							for provide, package in sorted(provides.items()):
								if provides_full:
									print(f'{provide:{provide_path_max_len}}\t{package.repo:{repo_max_len}} {package.name:{package_name_max_len}}\t{package.arch}\t{package.version}')
								else:
									print(f'{provide:{provide_path_max_len}}\t{package.repo:{repo_max_len}} {package.name}')

						case 'dot' | 'tree' | 'tree-full' | 'rdot' | 'rtree' | 'rtree-full':
							# print packages by package relations according to filters
							root_package = next(_iter_packages())
							if not root_package:
								print(f'Out format "dot" allowed only for one package. Define package name flter. See help: {argv[0]} -h')
								exit(-1)

							tree_full = args.out in ('tree-full', 'rtree-full')
							rtree = args.out in ('rtree', 'rtree-full')

							def _iter_required_packages(package: Package) -> Iterator[Package]:
								'iters packages that provides required relations by package'
								for package_ in packages:
									if any(package.iter_relations(package_, rtree)):
										yield package_

							def provides_packages(package: Package):
								nonlocal print_indent
								if package in provided_packages:
									return
								provided_packages.add(package)
								# process new package
								print_indent += '\t'
								if not package.requires:
									if args.verbose > 0:
										# print(print_indent, 'Requirements of package:', repo_name, package.name, '<None>')
										print(print_indent, '<None>')
									print_indent = print_indent[:-1]
									return
								# if args.verbose > 0:
								# 	print(print_indent, 'Requirements of package:', repo_name, package.name)
								# iter next level: required packages
								for provides_package in _iter_required_packages(package):
									# print(f'REL {requires_rel} {provides_package}')
									if provides_package in provided_packages:
										if tree_full:
											print(print_indent, provides_package.to_str(True, True), '=', '<ALREADY>', ', '.join(str(x[0]) for x in package.iter_relations(provides_package, rtree)))
									else:
										if args.verbose > 0:
											print(print_indent, provides_package.to_str(True, True), '=', ', '.join(str(x[0]) for x in package.iter_relations(provides_package, rtree)))
										# tree structure: next level
										provides_packages(provides_package)
								print_indent = print_indent[:-1]

							provided_packages: set[Package] = set()
							print_indent = ''
							# tree structure: root
							print(root_package.to_str())
							package_name_filter = None  # clear filter by package name
							packages = tuple(_iter_packages())  # get packages for tree
							print(f'Packages: {len(packages)}')
							provides_packages(root_package)

						case _:
							# text # print packages according to filters
							for i, package in enumerate(_iter_packages()):
								print_package(package, i+1)

			case 'filelists' | 'fl':
				# show filelists files
				for file_path in args.path:
					show_filelist(file_path)

			case 'primary' | 'pr':
				# show primary files
				for file_path in args.path:
					show_primary(file_path)

	try:
		main()
	except (KeyboardInterrupt, BrokenPipeError):
		exit(-1)
	except requests.exceptions.ConnectionError as e:
		print(e)
		exit(-1)
