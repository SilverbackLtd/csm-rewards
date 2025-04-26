import os

import httpx
from ape import Contract
from silverback import SilverbackBot
from silverback.exceptions import CircuitBreaker

# TODO: This should really be a package
from .tree import CSMRewardTree

# TODO: Support other IPFS clients?
ipfs = httpx.Client(
    base_url="https://ipfs.io/ipfs/",
    # NOTE: IPFS is finnicky, especially with new pins
    transport=httpx.HTTPTransport(retries=3),
    timeout=120,
)

# TODO: Telegram update support via aiogram?

bot = SilverbackBot()

# Eth Mainnet addresses
#TODO: Support other networks?
csm = Contract("0xdA7dE2ECdDfccC6c3AF10108Db212ACBBf9EA83F")
distributor = Contract("0xD99CC66fEC647E68294C6477B40fC7E0F6F618D0")

# TODO: Support multiple operators?
NODE_OPERATOR_ID = int(os.environ["NODE_OPERATOR_ID"])


def get_proof(tree, shares):
    leaf = tree.leaf((NODE_OPERATOR_ID, shares))
    index = tree.find(leaf)
    return list(tree.get_proof(index))


@bot.on_startup()
async def load_rewards_tree(_):
    tree_cid = distributor.treeCid()
    dumped_tree = ipfs.get(tree_cid).json()
    bot.state.tree = CSMRewardTree.load(dumped_tree)
    
    if bot.state.tree.root != distributor.treeRoot():
        raise CircuitBreaker("Corrupted Tree!")


@bot.on_(distributor.DistributionDataUpdated)
async def update_rewards_tree(log):
    dumped_tree = ipfs.get(log.treeCid).json()
    bot.state.tree = CSMRewardTree.load(dumped_tree)

    if bot.state.tree.root != distributor.treeRoot():
        raise CircuitBreaker("Corrupted Tree!")



@bot.on_(distributor.DistributionLogUpdated)
async def update_operator_metrics(log):
    distribution_log = ipfs.get(log.logCid).json()
    operator_data = distribution_log["operators"][NODE_OPERATOR_ID]
    validator_performance = (
        sum(
            v["included"] / v["assigned"]
            for v in operator_data["validators"].values()
        ) / len(operator_data["validators"])
    )

    return dict(
        validator_performance=validator_performance,
        fees_distributed=operator_data["distributed"] / 1e18,
        threshold=distribution_log["threshold"],
    )


# TODO: Monitor other aspects and measure rewards?


@bot.on_(distributor.FeeDistributed, nodeOperatorId=NODE_OPERATOR_ID)
async def fees_earned(log):
    """Fees distributed to our Operator"""
    return log.shares / 1e18


# Feature is only enabled if you add a bot signer
# NOTE: Signer must be either Manager or Reward address for Node Operator
if bot.signer:
    if os.environ.get("USE_WSTETH"):
        claim_method = csm.claimRewardsWstETH
    else:  # NOTE: normal stETH by default
        claim_method = csm.claimRewardsStETH

    # Default: claim the 1st of every month at 00:00 UTC
    @bot.cron(os.environ.get("REWARD_CLAIM_CRON", "0 0 1 * *"))
    async def claim_rewards(_):
        # NOTE: Just have it wait for the next time, because maybe we are updating it
        assert bot.state.tree.root == distributor.treeRoot(), "Tree not up to date!"

        operator_shares = next(
            fee_share
            for operator_id, fee_share in bot.state.tree
            if operator_id == NODE_OPERATOR_ID
        )

        claim_method(
            NODE_OPERATOR_ID,
            2 ** 256 - 1,  # As much as we can
            operator_shares,
            get_proof(bot.state.tree, operator_shares),
            sender=bot.signer,
            confirmations_required=0,
        )
