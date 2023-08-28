# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os

import yaml

from nemoguardrails.colang.v1_1.lang.grammar.load import load_lark_parser
from nemoguardrails.colang.v1_1.lang.transformer import ColangTransformer
from nemoguardrails.utils import CustomDumper

log = logging.getLogger(__name__)


class ColangParser:
    def __init__(self, include_source_mapping: bool = False):
        self.include_source_mapping = include_source_mapping
        self.grammar_path = os.path.join(
            os.path.dirname(__file__), "grammar", "colang.lark"
        )

        # Initialize the Lark Parser
        self._lark_parser = load_lark_parser(self.grammar_path)

    def get_parsing_tree(self, content: str):
        """Helper to get only the parsing tree.

        Args:
            content: The Colang content.

        Returns:
            An instance of a parsing tree as returned by Lark.
        """
        return self._lark_parser.parse(content + "\n")

    def parse_content(self, content: str, print_tokens=False):
        if print_tokens:
            tokens = list(self._lark_parser.lex(content))
            for token in tokens:
                print(token.__repr__())

        # NOTE: dealing with EOF is a bit tricky in Lark; the easiest solution
        # to avoid some issues arising from that is to append a new line at the end
        tree = self.get_parsing_tree(content)

        transformer = ColangTransformer(
            source=content, include_source_mapping=self.include_source_mapping
        )
        data = transformer.transform(tree)

        result = {"flows": []}

        # We take all the flow elements and return them
        for element in data["elements"]:
            if element["_type"] == "flow":
                result["flows"].append(element)

        return result


def parse_colang_file(filename: str, content: str, include_source_mapping: bool = True):
    """Parse the content of a .co."""

    colang_parser = ColangParser(include_source_mapping=include_source_mapping)
    result = colang_parser.parse_content(content, print_tokens=False)

    # flows = []
    # for flow_data in result["flows"]:
    #     # elements = parse_flow_elements(items)
    #     # TODO: extract the source code here
    #     source_code = ""
    #     flows.append(
    #         {
    #             "id": flow_data["name"],
    #             "elements": flow_data["elements"],
    #             "source_code": source_code,
    #         }
    #     )

    data = {
        "flows": result["flows"],
    }

    return data


def main():
    paths = [
        "../../../../tests/colang/parser/v1_1/inputs/test6.co",
    ]

    filenames = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for filename in files:
                    if filename.endswith(".co"):
                        filenames.append(os.path.join(root, filename))
        else:
            filenames.append(path)

    colang_parser = ColangParser()

    for filename in filenames:
        log.info("========================================")
        log.info(f"{filename}")
        log.info("========================================")
        with open(filename, "r") as file:
            content = file.read()

        tree = colang_parser.get_parsing_tree(content)
        log.info(tree.pretty())

        data = colang_parser.parse_content(content)
        print(yaml.dump(data, sort_keys=False, Dumper=CustomDumper, width=1000))


if __name__ == "__main__":
    main()