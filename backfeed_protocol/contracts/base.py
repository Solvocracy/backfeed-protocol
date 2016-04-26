from __future__ import division  # do division with floating point arithmetic
from datetime import datetime
from sqlalchemy import func

from ..models.user import User
from ..models.contribution import Contribution
from ..models.evaluation import Evaluation
from ..models.contract import Contract
from ..models import DBSession
from ..models import with_session


class BaseContract(Contract):
    """The BaseContract class defines functions common to all protocols.
    """
    __mapper_args__ = {
        'polymorphic_identity': 'base_contract'
    }
    USER_INITIAL_TOKENS = 50.0
    USER_INITIAL_REPUTATION = 20.0
    ALPHA = 0.7
    BETA = 0.5
    REFERRAL_REWARD_FRACTION = 0.2
    REFERRAL_TIMEFRAME = 30  # in days
    REWARD_TOKENS_TO_EVALUATORS = False

    CONTRIBUTION_TYPE = {
        u'base': {
            'fee': 1,
            'distribution_stake': 0.08,
            'reputation_reward_factor': 5,
            'reward_threshold': 0.5,
            'stake': 0.02,
            'token_reward_factor': 50,
            # the allowed values for votes:
            'evaluation_set': [0, 1],
        }
    }

    def _info(self):
        """return some information about the configuration of this contract"""
        import subprocess
        git_hash = subprocess.check_output(["git", "rev-parse", 'HEAD']).strip()
        return {
            'class': self.__class__,
            'engine': DBSession.get_bind(),
            'version': git_hash,
        }

    @with_session
    def create_user(self, tokens=None, reputation=None, referrer=None, referrer_id=None):
        """create a new user with default values"""
        if tokens is None:
            tokens = self.USER_INITIAL_TOKENS
        if reputation is None:
            reputation = self.USER_INITIAL_REPUTATION
        if referrer is None and referrer_id:
            referrer = self.get_user(referrer_id)
            if not referrer:
                raise ValueError('A user with id "{referrer_id}" could not be found'.format(referrer_id=referrer_id))
        new_user = User(
            contract=self,
            reputation=reputation,
            tokens=tokens,
            referrer=referrer,
        )
        return new_user

    @with_session
    def create_contribution(self, user, contribution_type=None):
        if not contribution_type:
            # as a default, we use the first contribution type that is
            # first in alphanumeric ordering. We might want to make this
            # configurable.
            contribution_types = self.CONTRIBUTION_TYPE.keys()
            contribution_types.sort()
            contribution_type = contribution_types[0]

        if contribution_type not in self.CONTRIBUTION_TYPE:
            msg = 'contribution_type "{contribution_type}" is not valid'.format(contribution_type=contribution_type)
            raise KeyError(msg)

        # the user pays the fee for the contribution
        if user.tokens < self.CONTRIBUTION_TYPE[contribution_type]['fee']:
            msg = 'User does not have enough tokens to pay the contribution fee.'
            raise Exception(msg)
        user.tokens = user.tokens - self.CONTRIBUTION_TYPE[contribution_type]['fee']

        new_contribution = Contribution(contract=self, user=user, contribution_type=contribution_type, token_fund=self.CONTRIBUTION_TYPE[contribution_type]['fee'])
        return new_contribution

    def is_evaluation_value_allowed(self, value, contribution_type='base'):
        return value in self.CONTRIBUTION_TYPE[contribution_type]['evaluation_set']  # need to modify in the case of continuous evaluations

    @with_session
    def create_evaluation(self, user, contribution, value):
        if not self.is_evaluation_value_allowed(value, contribution.contribution_type):
            msg = 'Evaluation value "{value}" is not valid'.format(value=value)
            raise ValueError(msg)

        previously_voted = False
        if self.get_evaluations(contribution_id=contribution.id, evaluator_id=user.id):
            previously_voted = True

        # disactivate any previous evaluations by this user
        for previous_evaluation in self.get_evaluations(
                contribution_id=contribution.id,
                evaluator_id=user.id):
            # TODO: we should not remove the evaluation from the database
            # because it will interest us for auditing
            DBSession.delete(previous_evaluation)

        evaluation = Evaluation(contract=self, user=user, contribution=contribution, value=value)

        # reward evaluator with tokens according to his reputation.
        if self.REWARD_TOKENS_TO_EVALUATORS:
            if not previously_voted:
                self.reward_evaluator_with_tokens(evaluation)

        #  have the user has to pay his fees to make the evaluation
        self.pay_evaluation_fee(evaluation)

        # update users reputation for previous, equally voting users
        self.reward_previous_evaluators(evaluation)

        self.reward_contributor(evaluation)

        return evaluation

    def reward_evaluator_with_tokens(self, evaluation):
        contribution = evaluation.contribution
        user = evaluation.user

        # calc relevant quantities for token reward
        evaluator_rep = user.reputation
        engaged_rep = contribution.engaged_reputation() - evaluator_rep  # contribution.engaged_reputation includes evaluator_rep
        total_rep = self.total_reputation()
        unvoted_rep = total_rep - engaged_rep

        if unvoted_rep > 0:
            # calc token reward
            token_reward = contribution.token_fund * (evaluator_rep / unvoted_rep)
            # update users tokens and deduct amount from fund.
            user.tokens += token_reward
            contribution.token_fund -= token_reward

    def pay_evaluation_fee(self, evaluation):
        """calculate the fee for the evaluator of this evaluation

        returns a pair (token_fee, repution_fee)
         """
        user = evaluation.user
        contribution = evaluation.contribution

        votedRep = user.reputation + contribution.engaged_reputation()
        votedRep = contribution.engaged_reputation()

        stakeFee = user.reputation * (1 - ((votedRep / self.total_reputation()) ** self.BETA))

        fee = self.CONTRIBUTION_TYPE[contribution.contribution_type]['stake'] * stakeFee
        evaluation.user.reputation -= fee
        # evaluation.user.save()

    def reward_previous_evaluators(self, evaluation):
        """award the evaluators of this contribution that have previously voted evaluation.value"""
        contribution = evaluation.contribution
        # find previous evaluations with the same value
        equallyVotedRep = self.sum_equally_voted_reputation(evaluation)
        if equallyVotedRep:
            # add the rep of the current user, because that is what the R code seems to do
            equallyVotedRep += evaluation.user.reputation
            stake_distribution = evaluation.user.reputation / equallyVotedRep
            burn_factor = (equallyVotedRep / self.total_reputation()) ** self.ALPHA
            # update previous voters
            for e in contribution.evaluations:
                if e.value == evaluation.value and e.user.id != evaluation.user.id:
                    new_reputation = e.user.reputation * \
                        (1 + (self.CONTRIBUTION_TYPE[contribution.contribution_type]['distribution_stake'] * stake_distribution * burn_factor))

                    reputation_delta = new_reputation - e.user.reputation
                    e.user.reputation = new_reputation
                    # reward the referrer
                    if e.user.referrer:
                        e.user.referrer.reputation += reputation_delta * self.REFERRAL_REWARD_FRACTION

    def sum_equally_voted_reputation(self, evaluation):
        """return the sum of reputation of evaluators of evaluation.contribution that
        have evaluated the same value"""
        equallyVotedRep = DBSession.query(func.sum(User.reputation)).\
            join(Evaluation).\
            filter(Evaluation.contribution_id == evaluation.contribution.id).\
            filter(Evaluation.value == evaluation.value).\
            one()[0]
        return equallyVotedRep - evaluation.user.reputation

    def contribution_score(self, contribution):
        total_reputation = self.total_reputation()
        upvotes = self.contribution_upvotes(contribution)
        downvotes = sum(e.user.reputation for e in contribution.evaluations if e.value == 0)
        downvotes = downvotes / total_reputation
        time_added = contribution.time
        time_delta = datetime.now() - time_added
        score = (1.0 - downvotes) * upvotes * (0.1 + 27 / (30 + time_delta.days))
        return score

    def contribution_upvotes(self, contribution):
        upvotes = sum(e.user.reputation for e in contribution.evaluations if e.value == 1)
        upvotes = upvotes / self.total_reputation()
        return upvotes

    def reward_contributor(self, evaluation):
        # don't reward anything if the value is 0/
        if evaluation.value == 0:
            return
        contribution = evaluation.contribution
        contributor = evaluation.contribution.user
        rewardBase = 0
        # currentScore = self.contribution_score(contribution)
        currentScore = self.contribution_upvotes(contribution)

        max_score = evaluation.contribution.max_score
        if currentScore > max_score:
            if max_score >= self.CONTRIBUTION_TYPE[contribution.contribution_type]['reward_threshold']:
                rewardBase = currentScore - max_score
            elif currentScore > self.CONTRIBUTION_TYPE[contribution.contribution_type]['reward_threshold']:
                rewardBase = currentScore
            contribution.max_score = currentScore

        if rewardBase > 0:
            # calc token reward by score/score_delta * tokenRewardFactor
            token_reward_factor = self.CONTRIBUTION_TYPE[contribution.contribution_type]['token_reward_factor']
            token_reward = token_reward_factor * rewardBase
            # calc token reward by score/score_delta * reputationRewardFactor
            reputation_reward_factor = self.CONTRIBUTION_TYPE[contribution.contribution_type]['reputation_reward_factor']
            reputationReward = reputation_reward_factor * rewardBase
            contributor.tokens = contributor.tokens + token_reward
            contributor.reputation = contributor.reputation + reputationReward

            if contributor.referrer:
                contributor.referrer.tokens += token_reward * self.REFERRAL_REWARD_FRACTION
                contributor.referrer.reputation += reputationReward * self.REFERRAL_REWARD_FRACTION

    def get_evaluation(self, evaluation_id):
        if evaluation_id is None:
            raise ValueError("evaluation_id cannot be None")
        return DBSession.query(Evaluation).get(evaluation_id)

    def get_evaluations(self, contribution_id=None, evaluator_id=None, value=None):
        qry = DBSession.query(Evaluation)
        if contribution_id:
            qry = qry.filter(Evaluation.contribution_id == contribution_id)
        if evaluator_id:
            qry = qry.filter(Evaluation.user_id == evaluator_id)
        if value is not None:
            qry = qry.filter(Evaluation.value == value)
        return qry.all()

    def get_users(self):
        return DBSession.query(User).all()

    def get_user(self, user_id):
        return DBSession.query(User).get(user_id)

    def get_contribution(self, contribution_id):
        if contribution_id is None:
            raise ValueError("contribution_id cannot be None")
        return DBSession.query(Contribution).get(contribution_id)

    def get_contributions(self, start=0, limit=None, contributor_id=None, order_by='-score'):
        query = DBSession.query(Contribution)
        # TODO: add 'score' as a column to the database, and do the ordering
        # from there
        # if order_by:
        #     query = query.order_by(order_by)
        # if limit:
        #     query = query.limit(limit)
        # if start:
        #     query = query.start(start)
        if order_by == 'score':
            results = query.all()

            def sort_key(x):
                return self.contribution_score(x)

            results.sort(key=sort_key)
        elif order_by == '-score':
            results = query.all()

            def sort_key(x):
                return self.contribution_score(x)

            results.sort(key=sort_key, reverse=True)
        elif order_by == 'time':
            query = query.order_by(Contribution.time)
            results = query.all()
        elif order_by == '-time':
            query = query.order_by(Contribution.time.desc())
            results = query.all()
        else:
            raise ValueError('Unknown "order_by" value: "{order_by}"'.format(order_by=order_by))
        results = results[start:]
        if limit:
            results = results[:limit]
        return results

    def contributions_count(self):
        return DBSession.query(Contribution).count()

    def total_reputation(self):
        return DBSession.query(func.sum(User.reputation)).one()[0]
