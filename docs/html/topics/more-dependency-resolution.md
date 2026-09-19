# More on Dependency Resolution

This article goes into more detail about pip's dependency resolution algorithm.
In certain situations, pip can take a long time to determine what to install,
and this article is intended to help readers understand what is happening
"behind the scenes" during that process.

## The dependency resolution problem

The process of finding a set of packages to install, given a set of dependencies
between them, is known to be an [NP-hard](https://en.wikipedia.org/wiki/NP-hardness)
problem. What this means in practice is roughly that the process scales
*extremely* badly as the size of the problem increases. So when you have a lot
of dependencies, working out what to install will, in the worst case, take a
very long time.

The practical implication of that is that there will always be some situations
where pip cannot determine what to install in a reasonable length of time. We
make every effort to ensure that such situations happen rarely, but eliminating
them altogether isn't even theoretically possible. We'll discuss what options
you have if you hit a problem situation like this a little later.

## Python specific issues

Dependency metadata is discovered during resolution. Pip can fetch separate
metadata files when the index provides them, read metadata from downloaded
distributions, or ask a source distribution's build backend to prepare it.

Fetching and preparing every available release would be costly. Pip instead
reads metadata as it considers candidates and backtracks when their
dependencies conflict.

## Dependency metadata

It is worth discussing precisely what metadata is needed in order to drive the
package resolution process. There are essentially three key pieces of
information:

* The project name
* The release version
* The dependencies themselves

There are other pieces of data (e.g., extras, python version restrictions, wheel
compatibility tags) which are used as well, but they do not fundamentally
alter the process, so we will ignore them here.

The most important information is the project name and version. Together with
its source, they identify a candidate for installation. Name and version must
be available from the moment the candidate object is created. This is not an
issue for distribution files (sdists and wheels) as that data is available from
the filename, but for unpackaged source trees, pip needs to call the build
backend to ask for that data. This is done before resolution proper starts.

The dependency data is *not* requested in advance (as noted above, doing so
would be prohibitively costly, and for a backtracking algorithm it isn't
needed). Instead, pip requests dependency data "on demand", as the algorithm
starts to check that particular candidate.

One particular implication of the lazy fetching of dependency data is that
often, pip *does not know* things that might be obvious to a human looking at
the dependency tree as a whole. For example, if package A depends on version
1.0 of package B, it's obvious to a human that there's no point in looking at
other versions of package B. But if pip starts looking at B before it has
considered A, it doesn't have access to A's dependency data, and so has no way
of knowing that looking at other versions of B is wasted work. And worse still,
pip cannot even know that there's vital information in A's dependencies.

This latter point is a common theme with many cases where pip takes a long time
to complete a resolution - there's information pip doesn't know at the point
where it makes a "wrong" choice. Most of the heuristics added to the resolver
to guide the algorithm are designed to guess correctly in the face of that
lack of knowledge.

## The resolver and the finder

So far, we have been talking about the "resolver" as a single entity. While that
is mostly true, the process of getting package data from an index is handled
by another component of pip, the "finder". The finder is responsible for
feeding candidates to the resolver, and has a key role to play in selecting
suitable candidates.

Candidates from local source directories and
{ref}`direct URL references <pypug:dependency-specifiers>` do not go through
the finder. They still participate in resolution, alongside candidates
obtained through the finder.

As well as determining what versions exist in the index for a given project,
the finder selects the best distribution file to use for that candidate. This
may be a wheel or a source distribution, and precisely what is selected is
controlled by wheel compatibility tags, pip's options (whether to prefer binary
or source) and metadata supplied by the index. In particular, if a file is
marked as only being for specific Python versions, the file will be ignored by
the finder (and the resolver may never even see that version).

The finder also provides candidates for a project to the resolver in order of
preference - the provider implements the rule that later versions are preferred
over older versions, for example.

## The resolver algorithm

Pip uses nab's PubGrub solver with a fast path and an automatic fallback.
Both use pip's package preparation and installation code. Pip chooses the path;
users do not need to select one.

### Fast path: fixed candidate sources

Requests start here, including named requirements, installed packages, and
local, editable or URL projects supplied on the command line or in requirements
files. The fast path does not require `--ignore-installed`.

Pip first tries a suitable installed version when the upgrade options allow it.
It reads that distribution's metadata and asks the finder for other versions
only when needed. Metadata for downloaded candidates is also prepared on demand.

For each package, this path works with a fixed set of choices during the solve.
That lets nab reuse dependency information across versions whose metadata is
known to agree. Installed versions are part of that set, including versions
that are no longer on the index. Before the finder has supplied the complete
list, dependency clauses stay tied to the selected version.

A local, editable or URL input fixes that project's source before solving.
Its metadata supplies dependencies in the same solve as installed and indexed
packages. Additional extras use that fixed source, and named requirements and
constraints must still accept its version. Pip does not list index alternatives
for a project whose source is fixed by an input.

### Fallback: changing candidate sources or admission

Some requests need choices that the fast path cannot represent. For example,
a dependency may introduce a URL containing another build of the same package
version, with different metadata. The fallback keeps those sources separate
and lets pip supply candidates as the requirements develop.

| Request or outcome | What pip does |
| --- | --- |
| Named requirements, installed packages, and local, editable or URL inputs | Starts with the fast path. |
| Dependency metadata introduces a URL, source replacement or unsupported preparation option | Retries the original request with the fallback. |
| The fast path cannot find a solution, or its result fails pip's checks | Retries with the fallback before reporting a resolution failure. |

An installed package whose dependencies require replacing that package also
uses the fallback. Hash/source constraints and some yanked-release cases need
its candidate checks. Invalid installed metadata, build failures and terminal index errors are
reported normally.

A failed fast attempt does not prove the request is impossible: metadata that
has not been read yet may supply a missing candidate. Fallback starts with fresh
solver state and the original requirements and options. Successful preparation
can be reused, but decisions and learned conflicts from the fast attempt are
discarded.

### Provider implementation

`CatalogueProvider` implements the fast path using ordinary version ranges.
It checks dependencies before committing a candidate, usually considers packages
with fewer matching choices first, and checks the completed selection against
pip's original requirements and candidate rules. A preferred installation can
be tried before counting finder choices. After repeated preparation of a
transitive package, it can restart once with command-line requirement order
taking precedence.

The fallback uses `NativeHost` and nab's `CandidateProvider`. Pip's factory
supplies candidates from installed distributions, indexes, direct URLs and
editable projects. The host attaches source identities to the version ranges,
so equal versions with different metadata remain separate choices.

Each fallback attempt creates a new host, provider and solver. A failed
catalogue solve goes directly to definitive native resolution. Other fallback
cases may first try provisional candidate availability and retry if those
assumptions fail validation. If a URL request conflicts with reused index
preparation, pip discards that preparation context and retries once to preserve
the URL's provenance.

Nab prioritizes packages involved in contextual query failures and can
temporarily demote repeated dependency blockers. Within that feedback ordering,
the native host uses the following preferences:

* Direct URL requirements.
* Exact pins using `===` or `==` without a wildcard.
* Upper version bounds using `<`, `<=`, `~=`, or `==` with a wildcard.
* Command-line requirement order.
* Other version restrictions, such as `>=` or `!=`.
* Package name.

The finder orders candidates within a package, subject to upgrade and
installed-package preferences. These choices happen before all transitive
metadata is available. In the diagram below, selecting between A->B and A->C may
happen before pip has read the dependencies shown in grey.

![](deps.png)
