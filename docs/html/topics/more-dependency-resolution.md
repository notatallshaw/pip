# More on Dependency Resolution

This article goes into more detail about pip's dependency resolution algorithm.
In certain situations, pip can take a long time to determine what to install,
and this article is intended to help readers understand what is happening
"behind the scenes" during that process.

```{note}
This document is a work in progress. The details included are accurate (at the
time of writing), but there is additional information, in particular around
pip's provider, which has not yet been included.

Contributions to improve this document are welcome.
```

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

Many algorithms for handling dependency resolution assume that you know the
full details of the problem at the start - that is, you know all of the
dependencies up front. Unfortunately, that is not the case for Python packages.
With the current package index structure, dependency metadata is only available
by downloading the package file, and extracting the data from it. And in the
case of source distributions, the situation is even worse as the project must
be built after being downloaded in order to determine the dependencies.

Work is ongoing to try to make metadata more readily available at lower cost,
but at the time of writing, this has not been completed.

As downloading projects is a costly operation, pip cannot pre-compute the full
dependency tree. This means that we are unable to use a number of techniques
for solving the dependency resolution problem. In practice, we have to use a
*backtracking algorithm*.

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

The most important information is the project name and version. Those two pieces
of information identify an individual "candidate" for installation, and must
uniquely identify such a candidate. Name and version must be available from the
moment the candidate object is created. This is not an issue for distribution
files (sdists and wheels) as that data is available from the filename, but for
unpackaged source trees, pip needs to call the build backend to ask for that
data. This is done before resolution proper starts.

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

Note that the resolver is *only* relevant for packages fetched from an index.
Candidates coming from other sources (local source directories, {ref}`direct
URL references <pypug:dependency-specifiers>`) do *not* go through the finder,
and are merged with the candidates provided by the finder as part of the resolver's
"provider" implementation.

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

The resolver itself is [nab](https://pypi.org/project/nab-resolver/), a
PubGrub implementation. PubGrub does not try candidate versions one at a
time and undo the last choice on failure. It records each requirement as an
incompatibility over version ranges, propagates those to derive the choices
they force, and on a contradiction derives a new incompatibility that
explains it, then jumps back to the decision that incompatibility implicates.
A range that has been ruled out is never revisited, and the same
incompatibilities are the proof pip renders when resolution fails.

nab knows nothing about Python packaging. Everything specific to Python is
supplied by pip through a provider:

* `choose_version` - given a package and the range still allowed for it,
  return the version to decide on. This is where the finder interacts with
  the resolver, and where yanked releases and prereleases are ruled on.
* `has_satisfying_version` - is there any usable version in this range? Asked
  without committing to one.
* `get_dependencies` - read a chosen version's metadata and return each
  dependency as a range. This is the implementation of getting and reading
  package metadata.
* `prioritize` - which undecided package to decide next. The heuristic that
  guides the search.
* `widen_decision` - after a version is ruled out, report the whole range
  that is ruled out for the same reason, so one derivation covers many
  versions instead of one.

Of these, `prioritize` is the one carrying the search heuristic. It orders
by how constrained a package is, so that packages with few remaining
candidates are decided before packages with many. As noted above, it is
doing this with limited information. See the following diagram:

![](deps.png)

When the provider is asked to choose between the red requirements (A->B and
A->C) it doesn't know anything about the dependencies of B or C (i.e., the
grey parts of the graph).

## Pinning and backtracking

The resolver decides a version for one package at a time. Each decision
narrows what is left, and propagation may then force further versions
without a decision at all. When a decision leads to a contradiction, the
conflict is analysed rather than merely undone: the versions responsible are
recorded as an incompatibility, so the same dead end is not re-entered by
another route.

This is what pip is doing when it reports that it is looking at multiple
versions of a package: the search has found a conflict and is working out
which earlier decision caused it.
