from bert.utils import START_OF_SENTENCE_TOKEN
from bert.utils import obtain_word_embeddings
from extractor.utils import BERT_OUTPUT_SIZE
from rouge import Rouge
from torch.distributions import Categorical

import numpy as np
import torch


class ActorCritic(torch.nn.Module):
    def __init__(self, n_features, n_hidden_units, extraction_model):
        """
        Todo: Research to see if Actor and Critic should share some layers
        Todo: Look into initialization
        :param n_features:
        :param n_outputs:
        :param n_hidden_units:
        """
        super(ActorCritic, self).__init__()

        # Actor
        self.extraction_model = extraction_model  # returns prob of extraction per sentence
        self.convert_to_dist = torch.nn.Sequential(
            Logit(),
            torch.nn.Softmax(dim=1)
        )

        self.softmax = torch.nn.Softmax(dim=2)
        self.sigmoid = torch.nn.Sigmoid()

        # Convert bert embeddings to custom embeddings
        self.tune_dim = 8
        self.bert_fine_tune = torch.nn.Linear(BERT_OUTPUT_SIZE, self.tune_dim, bias=False)

        self.bert_tokenizer = extraction_model.bert_tokenizer
        self.bert_model = extraction_model.bert_model
        self.init_hidden = torch.nn.Parameter(torch.rand(1, self.tune_dim), requires_grad=True)
        self.stop_embedding = torch.nn.Parameter(torch.rand(extraction_model.input_dim), requires_grad=True)

        self.gru = torch.nn.GRU(self.tune_dim * 2, self.tune_dim, batch_first=True)
        self.value_layer = torch.nn.Linear(self.tune_dim, 1)
        self.max_n_ext_sents = 4

    def add_stop_action(self, state, mask=None):
        # Add stop embedding:
        batch_size = state.shape[0]
        stop_embeddings = torch.cat([self.stop_embedding] * batch_size)
        stop_embeddings = stop_embeddings.view(batch_size, 1, -1)
        state = torch.cat([state, stop_embeddings], dim=1)

        # Don't mask out stop embedding
        if mask is not None:
            mask = torch.cat([mask, torch.ones(batch_size, 1)], dim=1)

        return state, mask

    def forward(self, state, mask, n_label_sents=None):
        action_dists, action_indicies, ext_sents = self.actor_layer(state, mask, n_label_sents)
        values = self.critic_layer(state, mask, ext_sents)
        return action_dists, action_indicies, values

    def actor_layer(self, batch_state, mask, n_label_sents=None):
        """
        :param batch_state:
        :param mask:
        :param n_label_sents: list containing number of extracted sentences in summary labels (oracle)
        :return:
        """
        # Obtain distribution amongst actions
        batch_state, mask = self.add_stop_action(batch_state, mask)
        batch_action_probs = self.extraction_model(batch_state)  # prob of extraction per sentence (binary)
        batch_action_probs = batch_action_probs * mask

        # Obtain number of samples in batch
        batch_size = batch_action_probs.shape[0]

        # Obtain maximum number of sentences to extract
        if n_label_sents is None:
            n_doc_sents = mask.sum(dim=1)
            batch_max_n_ext_sents = torch.tensor([self.max_n_ext_sents] * batch_size)
            batch_max_n_ext_sents = torch.min(batch_max_n_ext_sents.float(), n_doc_sents)
        else:
            batch_max_n_ext_sents = n_label_sents

        # Iterate through samples
        batch_action_dists, batch_action_indicies, batch_ext_sents = list(), list(), list()
        for sample_idx in range(batch_size):
            max_n_ext_sents = batch_max_n_ext_sents[sample_idx]
            action_probs = batch_action_probs[sample_idx:sample_idx+1]  # (1, n_actions)
            n_actions = action_probs.shape[1]
            stop_action_idx = n_actions - 1  # Previously appended stop_action embedding
            action_indicies, ext_sents, action_dists = list(), list(), list()

            # Extract sentences one at a time
            n_ext_sents = 0
            while True:
                # Obtain distribution amongst sentences to extract
                action_dist = self.convert_to_dist(action_probs)
                action_dist = Categorical(action_dist)

                # Sample sentence to extract
                action_idx = action_dist.sample()  # index of sentence to extract
                ext_sent = batch_state[sample_idx, action_idx:action_idx+1, :]  # embedding of sentence to extract
                action_probs[0, action_idx] = 0  # don't select sentence again

                # Collect
                action_dists.append(action_dist)
                action_indicies.append(action_idx)
                ext_sents.append(ext_sent)

                # Track number of sentences extracted from article
                n_ext_sents += 1

                # Check to see if should stop extracting sentences
                is_stop_action = action_idx >= stop_action_idx
                is_long_enough = n_ext_sents >= max_n_ext_sents
                if is_stop_action or is_long_enough:
                    break

            # Collect
            batch_action_dists.append(action_dists)  # Sentence extraction distribution at each time step
            batch_action_indicies.append(action_indicies)  # Extracted sentence index at each time step
            batch_ext_sents.append(ext_sents)  # Emebddings of extracted sentences at each time step

        # batch_action_dists = list(itertools.chain.from_iterable(batch_action_dists))
        batch_action_indicies = [torch.tensor(article_actions) for article_actions in batch_action_indicies]

        return batch_action_dists, batch_action_indicies, batch_ext_sents

    def critic_layer(self, batch_state, batch_mask, batch_extracted_sents):
        """
        :param batch_state: torch.tensor shape (batch_size, n_doc_sents, bert_dim)
        :param batch_mask: torch.tensor shape (batch_size, n_doc_sents, bert_dim)
        :param batch_extracted_sents: list(list(extracted_sent_embedding))
            extracted_sent_embedding: torch.tensor shape ()
        :return: torch.tensor of size (1, n_batch_ext_sents)
        """
        n_articles = len(batch_extracted_sents)
        batch_values = list()

        # Iterate over each article
        for j in range(n_articles):
            # Obtain single article in batch and its corresponding extracted sentences:
            state = batch_state[j:j+1]
            mask = batch_mask[j:j+1]
            state, mask = self.add_stop_action(state, mask)
            mask = (1 - mask).bool().unsqueeze(0)
            state = self.bert_fine_tune(state)  # (1, n_doc_sents, tune_dim)
            extracted_sents = batch_extracted_sents[j]

            # Obtain initial input_embedding: "[CLS]"
            input_embedding = torch.tensor(self.bert_tokenizer.encode(
                START_OF_SENTENCE_TOKEN
            )).unsqueeze(0)
            input_embedding = self.bert_model(input_embedding)[0]
            input_embedding = self.bert_fine_tune(input_embedding)  # (1, 1, tune_dim)

            # Initialize hidden state:
            hidden = self.init_hidden.unsqueeze(0)  # (1, 1, tune_dim)

            # Iterate over each extracted sent: Use PREVIOUSLY extracted sentence to calculate value
            values = list()
            n_ext_sents = len(extracted_sents)
            for i in range(n_ext_sents):
                # Obtain context (attention weighted document embeddings)
                attention = torch.bmm(hidden, state.transpose(1, 2))  # (1, 1, n_doc_sents)
                attention[mask] = -1e6  # assign low attention to things to mask
                attention_weight = self.softmax(attention)  # (1, 1, n_doc_sents)
                context = torch.bmm(attention_weight, state)  # (1, 1, tune_dim)

                # Obtain new hidden state
                rnn_input = torch.cat([input_embedding, context], dim=2)  # (1, 1, tune_dim * 2)
                __, hidden = self.gru(rnn_input, hidden)

                # Calculate value
                value = self.value_layer(hidden)  # (1, 1, 1)
                values.append(value)

                # Obtain PREVIOUSLY extracted sentence
                input_embedding = extracted_sents[i].unsqueeze(0)
                input_embedding = self.bert_fine_tune(input_embedding)  # (1, 1, 8)

            batch_values += values

        batch_values = torch.tensor(batch_values)  # (1, n_batch_ext_sents)
        return batch_values

    def evaluate(self, state, action):
        actor_output = self.actor_layer(state)
        dist = Categorical(actor_output)
        entropy = dist.entropy().mean()
        log_probs = dist.log_prob(action.view(-1))
        value = self.critic_layer(state)

        return log_probs, value, entropy


class RLModel:
    def __init__(
            self, extractor_model, abstractor_model, alpha=2e-5, gamma=0.99, batch_size=128):
        # Set attributes
        self.extractor_model = extractor_model
        self.abstractor_model = abstractor_model
        self.n_features = BERT_OUTPUT_SIZE
        self.softmax = torch.nn.Softmax(dim=1)

        # Hyper parameters
        self.alpha = alpha
        self.batch_size = batch_size
        self.gamma = gamma

        # Initialize weights
        self.n_hidden_units = 8
        self.policy_net = ActorCritic(
            n_features=self.n_features,
            n_hidden_units=self.n_hidden_units,
            extraction_model=self.extractor_model
        )

        # Optimizer
        self.optimizer = torch.optim.Adam(self.policy_net.parameters(), lr=self.alpha)

        # Todo: Remove all usages of stop embedding from this method and in rl_connection/train.py
        # Create stop embedding:
        self.stop_embedding = torch.nn.Parameter(torch.rand(BERT_OUTPUT_SIZE), requires_grad=True)

        self.rouge = Rouge()

    def sample_actions(self, state, mask):
        """
        Returns an actions for an entire trajectory

        :param state:
        :param mask:
        :return:
        """
        dists, actions, values = self.policy_net(state, mask)

        log_probs = list()
        for article_dists, article_actions in zip(dists, actions):
            log_prob = torch.tensor(
                [dist.log_prob(action) for dist, action in zip(article_dists, article_actions)]
            )
            log_probs.append(log_prob)
        log_probs = torch.cat(log_probs)

        # Return action
        return actions, log_probs, values

    def evaluate_state_and_action(self, state, action):
        """
        Obtain:
           - the probability of selection `action` when in input state 'state'
           - the value of the being in input state `state`
        :param action:
        :param state:
        :return:
        """
        # Add stop embedding:
        batch_size = state.shape[0]
        stop_embeddings = torch.cat([self.stop_embedding] * batch_size)
        state = torch.cat([state, stop_embeddings], dim=1)

        dist, value = self.policy_net(state)
        log_prob = dist.log_prob(action)
        return log_prob, value

    def get_gae(self, trajectory, lmbda=0.95):
        """
        :param trajectory:
        :param lmbda:
        :return:
        """
        # Todo: replace this codeblock
        rewards = [t[2] for t in trajectory]
        values = [t[-1] for t in trajectory]
        dummy_next_value = 0  # should get masked out
        values = values + [dummy_next_value]
        masks = [t[3] is not None for t in trajectory]  # next_state is not None

        gae = 0
        returns = []

        for step in reversed(range(len(rewards))):
            delta = rewards[step] + self.gamma * values[step + 1] * masks[step] - values[step]
            gae = delta + self.gamma * lmbda * masks[step] * gae
            returns.insert(0, gae + values[step])

        returns = torch.cat(returns)
        return returns

    def get_returns(self, trajectory):
        rewards = [t[2] for t in trajectory]
        is_terminals = [t[3] is None for t in trajectory]
        discounted_rewards = list()

        for reward, is_terminal in reversed(list(zip(rewards, is_terminals))):
            if is_terminal:
                discounted_reward = 0
            discounted_reward = reward + self.gamma * discounted_reward
            discounted_rewards.insert(0, [discounted_reward])

        discounted_rewards = torch.tensor(discounted_rewards)

        return discounted_rewards

    @staticmethod
    def select_random_batch(current_states, actions, log_probs, returns, advantages, mini_batch_size):
        random_indicies = np.random.randint(0, len(current_states), mini_batch_size)

        batch_current_states = current_states[random_indicies]
        batch_actions = actions[random_indicies]
        batch_log_probs = log_probs[random_indicies]
        batch_returns = returns[random_indicies]
        batch_advantages = advantages[random_indicies]

        return batch_current_states, batch_actions, batch_log_probs, batch_returns, batch_advantages

    def update(self, trajectory, clip_val=0.2):
        """
        :param trajectory:
        :param clip_val:
        :return:
        """
        # Extract from trajectory
        current_states, actions, rewards, next_states, old_log_probs, old_values = list(zip(*trajectory))
        current_states = torch.cat(current_states)
        old_log_probs = torch.cat(old_log_probs)
        old_values = torch.cat(old_values)
        returns = self.get_gae(trajectory)

        # Obtain advantages
        advantages = returns - old_values

        # Learn for each step in trajectory
        for _ in range(len(trajectory)):
            # Get random sample of experiences
            batch_current_state, batch_actions, batch_old_log_probs, batch_returns, batch_advantages = \
                self.select_random_batch(
                    current_states=current_states,
                    actions=actions,
                    log_probs=old_log_probs,
                    returns=returns,
                    advantages=advantages,
                    mini_batch_size=16
                )
            batch_old_log_probs = batch_old_log_probs.detach()
            batch_current_state = batch_current_state.detach()
            batch_actions = batch_actions.detach()

            new_log_probs, new_values, entropy = self.policy_net.evaluate(
                batch_current_state,
                batch_actions
            )

            # Calculate loss for actor
            ratio = (new_log_probs - batch_old_log_probs.detach()).exp().view(-1, 1)
            loss1 = ratio * batch_advantages.detach()
            loss2 = torch.clamp(ratio, 1 - clip_val, 1 + clip_val) * batch_advantages.detach()
            actor_loss = -torch.min(loss1, loss2).mean()

            # Calculate loss for critic
            sampled_returns = batch_returns.detach()
            critic_loss = (new_values - sampled_returns).pow(2).mean()

            # Credit: https://github.com/higgsfield/RL-Adventure-2/blob/master/3.ppo.ipynb
            overall_loss = 0.5 * critic_loss + actor_loss - 0.001 * entropy

            self.optimizer.zero_grad()
            overall_loss.backward()
            self.optimizer.step()

    def create_abstracted_sentences(
            self,
            batch_actions,
            source_documents,
            stop_action_index,
            teacher_forcing_pct=0.0,
            target_summary_embeddings=None
    ):
        """
        :param batch_actions:
        :param source_documents:
        :param stop_action_index:
        :param teacher_forcing_pct:
        :param target_summary_embeddings:
        :return:
        """
        # Obtain embeddings
        actions = list()
        for trajectory_actions in batch_actions:
            if trajectory_actions[-1] == stop_action_index:
                actions.append(trajectory_actions[:-1])
            else:
                actions.append(trajectory_actions)

        extracted_sentences = [
            np.array(source_doc)[a].tolist() for source_doc, a in zip(source_documents, actions)
        ]
        source_document_embeddings, __, __ = obtain_word_embeddings(
            self.extractor_model.bert_model,
            self.extractor_model.bert_tokenizer,
            extracted_sentences,
            static_embeddings=False
        )
        # Obtain extraction probability for each word in vocabulary
        word_probabilities = torch.exp(self.abstractor_model(
            source_document_embeddings,
            target_summary_embeddings,
            teacher_forcing_pct=teacher_forcing_pct
        )[0])  # (batch_size, n_target_words, vocab_size)

        # Get words with highest probability per time step
        chosen_words = torch.argmax(word_probabilities, dim=2)

        return chosen_words

    def determine_rewards(self, output_sentence, target_sentence, target_mask):
        """
        Uses ROUGE to calculate reward
        :param output_sentence:
        :param target_sentence:
        :param target_mask:
        :return:
        """
        n_target_words = target_mask[:, :, 0].sum(dim=1)

        # Convert target sentences indicies into words
        target_sentence = self.convert_to_words(target_sentence, n_target_words)

        # Convert output sentence indicies into words
        output_sentence = self.convert_to_words(output_sentence, n_target_words)

        # Compute ROUGE-L score
        scores = self.rouge.get_scores(output_sentence, target_sentence)
        scores = [score['rouge-l']['f'] for score in scores]

        return scores

    def convert_to_words(self, sentence_indicies, n_target_words):
        sentence_indicies = torch.roll(sentence_indicies, dims=1, shifts=-1)  # shift left
        bert_tokenizer = self.abstractor_model.bert_tokenizer

        sentence_words = [
            bert_tokenizer.convert_ids_to_tokens(idx) for idx in sentence_indicies.tolist()
        ]

        n_target_words = n_target_words.int()
        sentence_words = [" ".join(s[:n_words]) for s, n_words in zip(sentence_words, n_target_words)]

        return sentence_words




class Logit(torch.nn.Module):
    def __init__(self):
        super(Logit, self).__init__()

    def forward(self, x):
        return torch.log(x / (1 - x))
