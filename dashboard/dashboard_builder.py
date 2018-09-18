# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Creates a web page that shows the compatibility between packages as a grid.

For example:

------------------------------------------------------------
             |  absl-py | apache_beam | grpcio | tensorflow |
------------------------------------------------------------
absl-py      |   Good   |             |        |            |
apache_beam  |   Bad    |     Bad     |        |            |
grpcio       |   Good   |     Bad     |  Good  |            |
tensorflow   |   Good   |     Bad     |  Good  |    Good    |
------------------------------------------------------------
"""

import argparse
import datetime
import logging
import os
from typing import Any, Iterable, List, FrozenSet, Mapping, Tuple
import webbrowser

import jinja2

from compatibility_lib import configs
from compatibility_lib import compatibility_checker
from compatibility_lib import compatibility_store
from compatibility_lib import dependency_highlighter
from compatibility_lib import deprecated_dep_finder
from compatibility_lib import package

_JINJA2_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader('.'), autoescape=jinja2.select_autoescape())

_DEFAULT_INSTALL_NAMES = configs.PKG_LIST

SELF_SUCCESS = {'status': 'SUCCESS', 'self': True}


class _ResultHolder(object):
    def __init__(
        self,
        package_to_results:
            Mapping[package.Package,
                    List[compatibility_store.CompatibilityResult]],
        pairwise_to_results:
            Mapping[FrozenSet[package.Package],
                    List[compatibility_store.CompatibilityResult]],
        checker=None,
        store=None):
        self._package_to_results = package_to_results
        self._pairwise_to_results = pairwise_to_results
        self.checker = checker
        self.store = store
        self.deprecated_deps = self.get_deprecated_deps()

    def _is_py_version_incompatible(self, result):
        if result.status == compatibility_store.Status.INSTALL_ERROR:
            for version in [2, 3]:
                for pkg in result.packages:
                    major_version = result.python_major_version
                    name = pkg.install_name
                    unsupported = configs.PKG_PY_VERSION_NOT_SUPPORTED[version]

                    if major_version == version and name in unsupported:
                        return True
        return False

    def has_issues(self, p: package.Package) -> bool:
        """Returns true if the given package has any issues.
        
        Currently check for:
            1. Self compatibility
            2. Pairwise compatibility
        """
        # Get self result
        for package_2 in self._package_to_results.keys():
            p_and_package_2_result = self.get_result(p, package_2)
            # Don't report the package as having issues if it is purely the
            # result of a self-incompatibility of another package.
            package_2_self_conflict = False
            package_2_self_res = self.get_result(package_2, package_2)
            if SELF_SUCCESS not in package_2_self_res.get(
                    'self_compatibility_check'):
                package_2_self_conflict = True
            pair_res = p_and_package_2_result.get(
                'pairwise_compatibility_check')
            for result in pair_res:
                if not package_2_self_conflict and \
                                result['status'] != 'SUCCESS':
                    return True

        return False

    def get_deprecated_deps(self) -> Mapping[str, Tuple[List, bool]]:
        """
        Returns if there are deprecated dependencies for a
        given package as well as the list of deprecated deps for a package.
        """
        finder = deprecated_dep_finder.DeprecatedDepFinder(
            py_version='3', checker=self.checker, store=self.store)
        deprecated_deps = list(finder.get_deprecated_deps())

        results = {}
        for item in deprecated_deps:
            has_deprecated_deps = False
            (pkg_name, deps) = item[0]
            if deps:
                has_deprecated_deps = True
            results[pkg_name] = (deps, has_deprecated_deps)

        return results

    def has_deprecated_deps(self, p: package.Package) -> bool:
        return self.deprecated_deps[p.install_name][1]

    def needs_update(self, p: package.Package) -> bool:
        # Returns True if the given package needs update.
        pass

    def get_statistics(self, packages):
        """Get the total number of packages that has issues."""
        total_packages = len(configs.PKG_LIST)
        total_have_conflicts = 0
        total_have_deprecated_deps = 0
        total_needs_update = 0
        for pkg in packages:
            if self.has_issues(pkg):
                total_have_conflicts += 1
            if self.has_deprecated_deps(pkg):
                total_have_deprecated_deps += 1
            if self.needs_update(pkg):
                total_needs_update += 1

        return total_packages, total_have_conflicts,\
               total_have_deprecated_deps, total_needs_update

    def get_result(self,
                   package_1: package.Package,
                   package_2: package.Package) -> Mapping[str, Any]:
        """Returns the installation result of two packages.

        Args:
            package_1: One of the two packages to check installation
                compatibility with.
            package_2: One of the two packages to check installation
                compatibility with.

        Returns:
            The results of installing the two packages together, as a dict:

            {
                'status': <a compatibility_store.Status.value e.g. "SUCCESS">
                'self': <a bool indication whether the result is due to an
                         issue from within a single given package>
                'details': <a str representing the reason for any failure.
                            May be None.>
            }
        """
        self_result = []
        pair_result = []
        status_type = 'self-success'

        if (not self._package_to_results[package_1] or
            not self._package_to_results[package_2]):
            self_result.append(
                {
                    'status': compatibility_store.Status.UNKNOWN.name,
                    'self': True,
                }
            )
            status_type = 'self-unknown'

        package_results = (
                self._package_to_results[package_1] +
                self._package_to_results[package_2])

        for pr in package_results:
            if not self._is_py_version_incompatible(pr) and \
                            pr.status != compatibility_store.Status.SUCCESS:
                self_result.append(
                    {
                        'status': pr.status.value,
                        'self': True,
                        'details': pr.details
                    }
                )
                status_type = 'self-' + pr.status.value.lower()

        if package_1 == package_2:
            if not self_result:
                self_result.append(
                    {
                        'status': compatibility_store.Status.SUCCESS.name,
                        'self': True,
                    }
                )
        else:
            pairwise_results = self._pairwise_to_results[
                frozenset([package_1, package_2])]
            if not pairwise_results:
                pair_result.append(
                    {
                        'status': compatibility_store.Status.UNKNOWN.name,
                        'self': False,
                    }
                )
                status_type = 'pairwise-unknown'
            for pr in pairwise_results:
                if not self._is_py_version_incompatible(pr) and \
                            pr.status != compatibility_store.Status.SUCCESS:
                    pair_result.append(
                        {
                            'status': pr.status.value,
                            'self': False,
                            'details': pr.details
                        }
                    )
                    status_type = 'pairwise-' + pr.status.value.lower()

            if not pair_result:
                pair_result.append(
                    {
                        'status': compatibility_store.Status.SUCCESS.name,
                        'self': False,
                    }
                )
                if status_type is 'self-success':
                    status_type = 'pairwise-success'

        result = {
            'status_type': status_type,
            'self_compatibility_check': self_result,
            'pairwise_compatibility_check': pair_result
        }

        return result


class DashboardBuilder():
    """Build a web page that shows package compatibility status."""

    def __init__(self, packages: Iterable[package.Package],
                 results: _ResultHolder):
        self._packages = packages
        self._results = results

    def build_dashboard(self, template_name) -> str:
        """Returns a web page compatibility grid given a list of packages."""
        current_timestamp = datetime.datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S')
        template = _JINJA2_ENVIRONMENT.get_template(template_name)
        return template.render(
            packages=self._packages,
            results=self._results,
            current_timestamp=current_timestamp)


def main():
    parser = argparse.ArgumentParser(
        description='Display a grid show the dependency compatibility ' +
                    'between Python packages')
    parser.add_argument('--packages', nargs='+',
                        default=_DEFAULT_INSTALL_NAMES,
                        help='the packages to display compatibility ' +
                             'information for')
    parser.add_argument(
        '--browser',
        action='store_true',
        default=False,
        help='display the grid in a browser tab')

    args = parser.parse_args()

    checker = compatibility_checker.CompatibilityChecker()
    store = compatibility_store.CompatibilityStore()

    packages = [
        package.Package(install_name) for install_name in args.packages]
    package_to_results = store.get_self_compatibilities(packages)
    pairwise_to_results = store.get_compatibility_combinations(packages)
    results = _ResultHolder(
        package_to_results, pairwise_to_results, checker, store)

    dashboard_builder = DashboardBuilder(packages, results)

    # Build the pairwise grid dashboard
    logging.warning('Starting build the grid...')
    grid_html = dashboard_builder.build_dashboard(
        'dashboard/grid-template.html')
    grid_path = os.path.dirname(os.path.abspath(__file__)) + '/grid.html'
    with open(grid_path, 'wt') as f:
        f.write(grid_html)

    # Build the dashboard main page
    logging.warning('Starting build the main dashboard...')
    main_html = dashboard_builder.build_dashboard(
        'dashboard/main-template.html')

    main_path = os.path.dirname(os.path.abspath(__file__)) + '/index.html'
    with open(main_path, 'wt') as f:
        f.write(main_html)

    if args.browser:
        webbrowser.open_new_tab('file://' + main_path)


if __name__ == '__main__':
    main()